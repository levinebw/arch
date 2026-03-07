#!/usr/bin/env python3
"""
Mock claude CLI for automated UAT testing.

Replaces the real `claude` binary. Reads the MCP config from args,
connects to the ARCH MCP server via SSE, and executes a scripted
sequence of MCP tool calls based on the agent's role (detected from
the prompt or CLAUDE.md in the worktree).

Outputs valid stream-json to stdout so the token tracker and session
manager work normally.

Usage (invoked by session.py, not directly):
    mock_claude --model X --output-format stream-json --verbose \
                --mcp-config /path/to/config.json --print "prompt..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Any, Optional


def emit_stream_json(event: dict) -> None:
    """Write a stream-json event to stdout."""
    print(json.dumps(event), flush=True)


def emit_system_message(text: str) -> None:
    """Emit a system event."""
    emit_stream_json({"type": "system", "message": text})


def emit_assistant_message(text: str) -> None:
    """Emit an assistant text event."""
    emit_stream_json({
        "type": "assistant",
        "message": {"role": "assistant", "content": text},
    })


def emit_tool_use(tool_name: str, tool_input: dict) -> None:
    """Emit a tool_use event."""
    emit_stream_json({
        "type": "tool_use",
        "name": tool_name,
        "input": tool_input,
    })


def emit_result(session_id: str) -> None:
    """Emit a final result event with token usage."""
    emit_stream_json({
        "type": "result",
        "session_id": session_id,
        "usage": {
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_tokens": 100,
            "cache_creation_tokens": 50,
        },
        "cost_usd": 0.005,
        "duration_ms": 3000,
        "is_error": False,
        "num_turns": 1,
    })


class MCPClient:
    """Minimal MCP client that connects via SSE and calls tools.

    The MCP SSE protocol works as follows:
    1. GET /sse/{agent_id} — SSE stream; first event is 'endpoint' with POST URL
    2. POST JSON-RPC to that URL — returns 202 Accepted (empty)
    3. Response arrives as 'message' event on the SSE stream
    """

    def __init__(self, mcp_config_path: str):
        with open(mcp_config_path) as f:
            config = json.load(f)

        server_config = config["mcpServers"]["arch"]
        self.sse_url = server_config["url"]
        self._session_url: Optional[str] = None
        self._pending_responses: dict[str, Any] = {}
        self._response_events: dict[str, Any] = {}
        import threading
        self._lock = threading.Lock()

    def connect(self, timeout: float = 15.0) -> bool:
        """Connect to the SSE endpoint and get the session URL."""
        import threading

        self._connected = threading.Event()
        self._error: Optional[str] = None

        def _read_sse():
            try:
                req = urllib.request.Request(self.sse_url)
                req.add_header("Accept", "text/event-stream")
                resp = urllib.request.urlopen(req, timeout=300)
                event_type = ""

                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if event_type == "endpoint":
                            base = self.sse_url.rsplit("/sse/", 1)[0]
                            self._session_url = base + data
                            self._connected.set()
                        elif event_type == "message":
                            # JSON-RPC response delivered via SSE
                            try:
                                msg = json.loads(data)
                                msg_id = str(msg.get("id", ""))
                                with self._lock:
                                    self._pending_responses[msg_id] = msg
                                    if msg_id in self._response_events:
                                        self._response_events[msg_id].set()
                            except json.JSONDecodeError:
                                pass
                    elif line == "":
                        continue
            except Exception as e:
                if not self._connected.is_set():
                    self._error = str(e)
                    self._connected.set()

        self._sse_thread = threading.Thread(target=_read_sse, daemon=True)
        self._sse_thread.start()

        self._connected.wait(timeout=timeout)
        if self._error:
            log(f"SSE connection error: {self._error}")
            return False
        if not self._session_url:
            log("SSE connection: no session URL received")
            return False

        log(f"Connected to MCP server, session URL: {self._session_url}")
        return True

    def initialize(self) -> dict:
        """Send MCP initialize request."""
        result = self._send_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mock-claude", "version": "1.0"},
        })
        # Send initialized notification (no response expected)
        self._send_notification("notifications/initialized", {})
        return result

    def call_tool(self, name: str, arguments: dict) -> Any:
        """Call an MCP tool and return the result."""
        result = self._send_jsonrpc("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result

    def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if not self._session_url:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._session_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def _send_jsonrpc(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and wait for the response via SSE."""
        if not self._session_url:
            log("No session URL - not connected")
            return None

        import threading
        request_id = str(uuid.uuid4())[:8]

        # Set up response event before sending
        event = threading.Event()
        with self._lock:
            self._response_events[request_id] = event

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._session_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )

        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"POST error for {method}: {e}")
            with self._lock:
                self._response_events.pop(request_id, None)
            return None

        # Wait for response on SSE stream
        if not event.wait(timeout=timeout):
            log(f"Timeout waiting for response to {method} (id={request_id})")
            with self._lock:
                self._response_events.pop(request_id, None)
            return None

        with self._lock:
            self._response_events.pop(request_id, None)
            msg = self._pending_responses.pop(request_id, None)

        if not msg:
            return None

        if "result" in msg:
            return msg["result"]
        if "error" in msg:
            log(f"JSON-RPC error for {method}: {msg['error']}")
            return msg["error"]
        return msg


def log(msg: str) -> None:
    """Log to stderr (visible in test output)."""
    print(f"[mock-claude] {msg}", file=sys.stderr, flush=True)


def detect_role(prompt: str, cwd: str) -> str:
    """Detect agent role from prompt or CLAUDE.md."""
    prompt_lower = prompt.lower()

    # Check prompt for role hints
    if "archie" in prompt_lower or "lead agent" in prompt_lower or "coordinator" in prompt_lower:
        return "archie"
    if "frontend" in prompt_lower:
        return "frontend"
    if "qa" in prompt_lower or "test" in prompt_lower:
        return "qa"
    if "backend" in prompt_lower:
        return "backend"

    # Check CLAUDE.md in worktree
    claude_md = Path(cwd) / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text().lower()
        if "archie" in content or "lead agent" in content:
            return "archie"
        if "frontend" in content:
            return "frontend"
        if "qa" in content:
            return "qa"
        if "backend" in content:
            return "backend"

    return "worker"


def _extract_text(result: Any) -> str:
    """Extract text from an MCP tool result."""
    if not result or not isinstance(result, dict):
        return ""
    content = result.get("content", [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
    return str(result)


def _extract_available_roles(context_text: str, cwd: str) -> list[str]:
    """Discover available agent roles from arch.yaml in the project."""
    # Try to find arch.yaml by walking up from CWD
    # (agent runs in worktree, arch.yaml is in the parent project)
    search_dirs = [Path(cwd)]
    # Also check repo_path from the context
    try:
        data = json.loads(context_text)
        repo_path = data.get("repo_path", "")
        if repo_path:
            search_dirs.insert(0, Path(repo_path))
    except (json.JSONDecodeError, TypeError):
        pass

    for d in search_dirs:
        config_path = d / "arch.yaml"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    import yaml
                    config = yaml.safe_load(f)
                pool = config.get("agent_pool", [])
                return [entry["id"] for entry in pool if isinstance(entry, dict) and "id" in entry]
            except Exception as e:
                log(f"Error reading arch.yaml: {e}")

    return []


ROLE_ASSIGNMENTS = {
    "frontend": "Build the HTML pages as specified in BRIEF.md. Create index.html with nav, commit to git.",
    "qa": "Write test_site.py to validate HTML structure per BRIEF.md. Commit to git.",
    "backend": "Build the backend API as specified in BRIEF.md. Commit to git.",
}


def run_archie_script(client: MCPClient, prompt: str, cwd: str) -> None:
    """Archie's scripted behavior: read context, spawn available agents, monitor."""
    log("Running ARCHIE script")

    # Step 1: Get project context to discover available roles
    emit_assistant_message("Let me start by reading the project context.")
    emit_tool_use("get_project_context", {})
    result = client.call_tool("get_project_context", {})
    context_text = _extract_text(result)
    log(f"get_project_context text: {context_text[:300]}")
    time.sleep(0.5)

    available_roles = _extract_available_roles(context_text, cwd)
    log(f"Available roles from config: {available_roles}")

    # Fallback: try common roles if we can't parse the context
    if not available_roles:
        available_roles = ["frontend", "qa"]
        log("Could not parse roles from context, using defaults")

    # Step 2: Spawn each available agent
    for role in available_roles:
        assignment = ROLE_ASSIGNMENTS.get(role, f"Complete the {role} work as specified in BRIEF.md. Commit to git.")
        emit_assistant_message(f"I'll spawn a {role} agent.")
        emit_tool_use("spawn_agent", {"role": role, "assignment": assignment})
        result = client.call_tool("spawn_agent", {"role": role, "assignment": assignment})
        log(f"spawn_agent({role}) result: {json.dumps(result)[:200] if result else 'None'}")
        time.sleep(0.5)

    # Step 3: Check agent status
    emit_assistant_message("Let me check on the agents.")
    emit_tool_use("list_agents", {})
    result = client.call_tool("list_agents", {})
    log(f"list_agents result: {json.dumps(result)[:200] if result else 'None'}")

    # Archie exits after spawning. The orchestrator's auto-resume
    # will restart Archie when agents send report_completion messages.
    emit_assistant_message("Agents are working. I'll wait for their reports.")


def run_worker_script(client: MCPClient, role: str, prompt: str, cwd: str) -> None:
    """Worker agent script: update status, do work, commit, report completion."""
    log(f"Running WORKER script for role={role}")

    # Step 1: Update status
    emit_assistant_message(f"Starting work as {role} agent.")
    emit_tool_use("update_status", {"status": "working", "task": f"Building {role} deliverables"})
    client.call_tool("update_status", {"status": "working", "task": f"Building {role} deliverables"})
    time.sleep(0.3)

    # Step 2: Send message to archie
    emit_tool_use("send_message", {"to": "archie", "content": f"{role} agent starting work."})
    client.call_tool("send_message", {"to": "archie", "content": f"{role} agent starting work."})
    time.sleep(0.3)

    # Step 3: Do actual work (create files and commit)
    if role == "frontend":
        _create_frontend_files(cwd)
    elif role == "qa":
        _create_qa_files(cwd)
    else:
        _create_generic_files(cwd, role)

    # Git commit
    import subprocess
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_AUTHOR_NAME"] = f"{role}-agent"
    env["GIT_AUTHOR_EMAIL"] = f"{role}@arch-test.com"
    env["GIT_COMMITTER_NAME"] = f"{role}-agent"
    env["GIT_COMMITTER_EMAIL"] = f"{role}@arch-test.com"

    subprocess.run(["git", "add", "."], cwd=cwd, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", f"{role}: deliver assigned work"],
        cwd=cwd, capture_output=True, env=env,
    )
    log(f"Git commit done in {cwd}")

    # Step 4: Report completion
    artifacts = {
        "frontend": ["index.html", "projects.html", "contact.html"],
        "qa": ["test_site.py"],
    }.get(role, ["output.txt"])

    emit_assistant_message(f"Work complete. Reporting to Archie.")
    emit_tool_use("report_completion", {
        "summary": f"{role} deliverables complete.",
        "artifacts": artifacts,
    })
    client.call_tool("report_completion", {
        "summary": f"{role} deliverables complete.",
        "artifacts": artifacts,
    })
    time.sleep(0.3)


def run_archie_resume_script(client: MCPClient) -> None:
    """Archie resumed to handle completion reports. Merge and close."""
    log("Running ARCHIE RESUME script")

    # Read messages
    emit_assistant_message("Let me check messages from agents.")
    emit_tool_use("get_messages", {})
    result = client.call_tool("get_messages", {})
    log(f"get_messages result: {json.dumps(result)[:300] if result else 'None'}")
    time.sleep(0.3)

    # List agents to see who's done
    emit_tool_use("list_agents", {})
    agents_result = client.call_tool("list_agents", {})
    log(f"list_agents result: {json.dumps(agents_result)[:300] if agents_result else 'None'}")
    time.sleep(0.3)

    # Parse agent list to find completed agents
    completed_agents = []
    agents_text = _extract_text(agents_result)
    log(f"list_agents text: {agents_text[:500]}")
    try:
        agents_data = json.loads(agents_text)
        # Handle both {"agents": [...]} and [...] formats
        agent_list = agents_data.get("agents", agents_data) if isinstance(agents_data, dict) else agents_data
        if isinstance(agent_list, list):
            for a in agent_list:
                if isinstance(a, dict) and a.get("status") == "done" and a.get("id") != "archie":
                    completed_agents.append(a["id"])
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        log(f"Failed to parse agents: {e}")

    log(f"Completed agents to merge: {completed_agents}")

    # Try to merge and tear down each completed agent
    for agent_id in completed_agents:
        emit_assistant_message(f"Merging work from {agent_id}.")
        emit_tool_use("request_merge", {"agent_id": agent_id, "target_branch": "main"})
        client.call_tool("request_merge", {"agent_id": agent_id, "target_branch": "main"})
        time.sleep(0.5)

        emit_tool_use("teardown_agent", {"agent_id": agent_id})
        client.call_tool("teardown_agent", {"agent_id": agent_id})
        time.sleep(0.3)

    # Close project
    emit_assistant_message("All work merged. Closing project.")
    emit_tool_use("close_project", {"summary": "All agents completed and merged. Project done."})
    client.call_tool("close_project", {"summary": "All agents completed and merged. Project done."})


def _create_frontend_files(cwd: str) -> None:
    """Create frontend deliverables."""
    nav = """<nav><a href="index.html">Home</a> | <a href="projects.html">Projects</a> | <a href="contact.html">Contact</a></nav>"""
    style = """<style>body{font-family:sans-serif;max-width:800px;margin:0 auto;padding:20px}nav{background:#333;padding:10px}nav a{color:white;margin-right:15px;text-decoration:none}h1{color:#333}.project{border:1px solid #ddd;padding:15px;margin:10px 0;border-radius:5px}form label{display:block;margin:10px 0 5px}form input,form textarea{width:100%;padding:8px;border:1px solid #ccc;border-radius:3px}form button{background:#333;color:white;padding:10px 20px;border:none;border-radius:3px;margin-top:10px;cursor:pointer}</style>"""

    Path(cwd, "index.html").write_text(f"""<!DOCTYPE html>
<html><head><title>Portfolio</title>{style}</head><body>
{nav}
<h1>Jane Developer</h1>
<p>Full-stack developer building great software.</p>
</body></html>""")

    Path(cwd, "projects.html").write_text(f"""<!DOCTYPE html>
<html><head><title>Projects</title>{style}</head><body>
{nav}
<h1>Projects</h1>
<div class="project"><h2>Project Alpha</h2><p>A web application for task management.</p></div>
<div class="project"><h2>Project Beta</h2><p>An API gateway for microservices.</p></div>
<div class="project"><h2>Project Gamma</h2><p>A real-time dashboard for analytics.</p></div>
</body></html>""")

    Path(cwd, "contact.html").write_text(f"""<!DOCTYPE html>
<html><head><title>Contact</title>{style}</head><body>
{nav}
<h1>Contact Me</h1>
<form><label>Name</label><input type="text" name="name"><label>Email</label><input type="email" name="email"><label>Message</label><textarea name="message" rows="5"></textarea><button type="submit">Send</button></form>
</body></html>""")

    log("Created index.html, projects.html, contact.html")


def _create_qa_files(cwd: str) -> None:
    """Create QA test file."""
    Path(cwd, "test_site.py").write_text('''#!/usr/bin/env python3
"""Validate portfolio site HTML structure."""
import os
import re
import unittest

class TestPortfolioSite(unittest.TestCase):
    """Tests for the multi-page portfolio site."""

    def test_all_html_files_exist(self):
        for f in ["index.html", "projects.html", "contact.html"]:
            self.assertTrue(os.path.exists(f), f"{f} not found")

    def test_navigation_links(self):
        for f in ["index.html", "projects.html", "contact.html"]:
            if os.path.exists(f):
                content = open(f).read()
                self.assertIn("<nav", content, f"{f} missing nav")

    def test_contact_form_fields(self):
        if os.path.exists("contact.html"):
            content = open("contact.html").read()
            self.assertIn("name=\\"name\\"", content, "Missing name field")
            self.assertIn("name=\\"email\\"", content, "Missing email field")
            self.assertIn("name=\\"message\\"", content, "Missing message field")

    def test_projects_count(self):
        if os.path.exists("projects.html"):
            content = open("projects.html").read()
            projects = re.findall(r"class=\\"project\\"", content)
            self.assertGreaterEqual(len(projects), 3, "Need at least 3 projects")

    def test_inter_page_links(self):
        for f in ["index.html", "projects.html", "contact.html"]:
            if os.path.exists(f):
                content = open(f).read()
                self.assertIn("index.html", content)

if __name__ == "__main__":
    unittest.main()
''')
    log("Created test_site.py")


def _create_generic_files(cwd: str, role: str) -> None:
    """Create a generic output file for unknown roles."""
    Path(cwd, "output.txt").write_text(f"Output from {role} agent.\n")


def parse_args() -> argparse.Namespace:
    """Parse claude CLI args (enough to extract what we need)."""
    parser = argparse.ArgumentParser(description="Mock claude CLI")
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--mcp-config", required=True)
    parser.add_argument("--print", dest="print_mode", action="store_true")
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--allowedTools", nargs="*", default=[])
    parser.add_argument("--permission-prompt-tool", default=None)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("prompt", nargs="?", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cwd = os.getcwd()
    session_id = str(uuid.uuid4())[:12]

    log(f"Mock claude started: model={args.model}, cwd={cwd}")
    log(f"MCP config: {args.mcp_config}")
    log(f"Prompt: {args.prompt[:100]}...")
    log(f"Resume: {args.resume}")

    # Emit initial stream-json events
    emit_system_message("Mock claude session started")

    # Connect to MCP server
    client = MCPClient(args.mcp_config)
    if not client.connect():
        log("Failed to connect to MCP server")
        emit_stream_json({"type": "error", "message": "MCP connection failed"})
        emit_result(session_id)
        sys.exit(1)

    # Initialize MCP session
    init_result = client.initialize()
    log(f"MCP initialize: {json.dumps(init_result)[:200] if init_result else 'None'}")

    # Detect role and run appropriate script
    role = detect_role(args.prompt, cwd)
    is_resume = args.resume is not None

    log(f"Detected role: {role}, is_resume: {is_resume}")

    try:
        if role == "archie":
            if is_resume:
                run_archie_resume_script(client)
            else:
                run_archie_script(client, args.prompt, cwd)
        else:
            run_worker_script(client, role, args.prompt, cwd)
    except Exception as e:
        log(f"Script error: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)

    # Emit final result
    emit_result(session_id)
    log("Mock claude exiting normally")


if __name__ == "__main__":
    main()
