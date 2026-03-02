"""
End-to-end integration tests for ARCH.

These tests exercise the FULL data flow:
  Orchestrator startup → real MCP server (uvicorn) → MCP SSE client connects
  as agent → tool calls route through MCP → orchestrator callbacks fire →
  state/worktrees updated → shutdown.

Real components: git, StateStore, WorktreeManager, MCPServer (uvicorn), SSE clients.
Mocked: claude CLI subprocess (create_subprocess_exec), Docker, GitHub gate.
"""

import asyncio
import json
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mcp.client.sse import sse_client
from mcp import ClientSession

from arch.orchestrator import Orchestrator


# ============================================================================
# Helpers
# ============================================================================


def get_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def create_mock_claude_process(session_id="test-session"):
    """Create a mock Claude subprocess with stream-json output.

    Returns a fresh mock each call so multiple agents don't share stdout.
    """
    mock = MagicMock()
    mock.pid = id(mock) % 100000  # unique pid per mock
    mock.returncode = None

    output_lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working."}]}}),
        json.dumps({"type": "usage", "input_tokens": 500, "output_tokens": 200,
                     "cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}),
        json.dumps({"type": "result", "session_id": session_id}),
    ]

    output_iter = iter(output_lines + [None])

    async def readline():
        try:
            line = next(output_iter)
            if line is None:
                return b""
            return (line + "\n").encode()
        except StopIteration:
            return b""

    mock.stdout = MagicMock()
    mock.stdout.readline = readline
    mock.stderr = MagicMock()
    mock.stderr.readline = AsyncMock(return_value=b"")
    mock.wait = AsyncMock(return_value=0)
    mock.terminate = MagicMock()
    mock.kill = MagicMock()

    return mock


@contextmanager
def mock_subprocess_only():
    """Mock subprocess and Docker/GitHub gates — MCP server runs for real.

    Unlike test_integration.py's mock_external_tools, this does NOT mock
    MCPServer.start/stop. The real uvicorn server starts and binds.

    Uses side_effect to return a FRESH mock process per create_subprocess_exec
    call so Archie and each worker agent get independent stdout streams.
    """
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.side_effect = lambda *a, **kw: create_mock_claude_process()

        with patch("arch.orchestrator.check_docker_available", return_value=(True, "OK")):
            with patch("arch.orchestrator.check_image_exists", return_value=True):
                with patch("arch.orchestrator.check_github_gate",
                           return_value=(True, "GitHub: test/repo")):
                    with patch("arch.container.check_image_exists", return_value=True):
                        with patch("arch.container.pull_image",
                                   return_value=(True, "Image pulled")):
                            yield mock_exec


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def e2e_repo(tmp_path):
    """Create a real git repository for E2E testing."""
    repo_path = tmp_path / "e2e-project"
    repo_path.mkdir()

    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(tmp_path)}

    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True,
                    check=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@e2e.com"],
                    cwd=repo_path, capture_output=True, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "E2E Test"],
                    cwd=repo_path, capture_output=True, check=True, env=env)

    # Initial commit
    (repo_path / "README.md").write_text("# E2E Test Project\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True,
                    check=True, env=env)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                    cwd=repo_path, capture_output=True, check=True, env=env)

    return repo_path


@pytest.fixture
def e2e_config(e2e_repo, tmp_path):
    """Create arch.yaml with a free port for real MCP server."""
    port = get_free_port()
    state_dir = tmp_path / "state"
    personas_dir = e2e_repo / "personas"
    personas_dir.mkdir()

    (personas_dir / "archie.md").write_text("# Archie\nYou are the lead agent.\n")
    (personas_dir / "frontend.md").write_text("# Frontend Dev\nYou build UIs.\n")
    (personas_dir / "backend.md").write_text("# Backend Dev\nYou build APIs.\n")

    (e2e_repo / "BRIEF.md").write_text("# Brief\n\n## Goals\nE2E test.\n\n"
                                        "## Done When\n- Tests pass\n\n"
                                        "## Constraints\n- None\n\n"
                                        "## Current Status\nTesting.\n\n"
                                        "## Decisions Log\n| Date | Decision | Rationale |\n"
                                        "|------|----------|----------|\n")

    config = {
        "project": {
            "name": "E2E Test Project",
            "description": "Testing full lifecycle",
            "repo": str(e2e_repo),
        },
        "archie": {
            "persona": "personas/archie.md",
            "model": "claude-sonnet-4-6",
        },
        "agent_pool": [
            {
                "id": "frontend-dev",
                "persona": "personas/frontend.md",
                "model": "claude-sonnet-4-6",
                "max_instances": 2,
            },
            {
                "id": "backend-dev",
                "persona": "personas/backend.md",
                "model": "claude-sonnet-4-6",
                "max_instances": 1,
            },
        ],
        "settings": {
            "state_dir": str(state_dir),
            "mcp_port": port,
            "max_concurrent_agents": 5,
            "token_budget_usd": 10.0,
        },
    }

    config_path = e2e_repo / "arch.yaml"
    config_path.write_text(yaml.dump(config))

    return config_path, port


# ============================================================================
# Tests
# ============================================================================


class TestE2ELifecycle:
    """Full lifecycle tests with real MCP server + orchestrator."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, e2e_config, e2e_repo):
        """Startup → spawn agent → messaging → completion → teardown → shutdown.

        This is the centerpiece E2E test. Exercises the full data flow:
        real MCP server, real git worktrees, real state persistence,
        with test code acting as the agents via MCP SSE clients.
        """
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)  # Let uvicorn fully bind

            try:
                # === Archie connects and spawns a worker ===
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        # Archie discovers tools
                        tools = await archie.list_tools()
                        tool_names = {t.name for t in tools.tools}
                        assert "spawn_agent" in tool_names
                        assert "send_message" in tool_names
                        assert "teardown_agent" in tool_names
                        assert "handle_permission_request" in tool_names

                        # Archie spawns a frontend worker
                        result = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Build the login page with email/password fields",
                        })
                        data = json.loads(result.content[0].text)
                        assert "agent_id" in data, f"spawn_agent failed: {data}"
                        agent_id = data["agent_id"]
                        worktree_path = Path(data["worktree_path"])

                # === Verify worktree and state were created ===
                assert worktree_path.exists(), "Worktree directory should exist"
                claude_md = worktree_path / "CLAUDE.md"
                assert claude_md.exists(), "CLAUDE.md should be written in worktree"
                claude_md_content = claude_md.read_text()
                assert "login page" in claude_md_content.lower(), \
                    "CLAUDE.md should contain the assignment"

                agent = orch.state.get_agent(agent_id)
                assert agent is not None, "Agent should be registered in state"
                assert agent["role"] == "frontend-dev"

                # === Worker connects, sends message, reports completion ===
                async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                    async with ClientSession(r, w) as worker:
                        await worker.initialize()

                        # Worker should NOT have Archie-only tools
                        worker_tools = await worker.list_tools()
                        worker_names = {t.name for t in worker_tools.tools}
                        assert "send_message" in worker_names
                        assert "spawn_agent" not in worker_names

                        # Worker sends status update
                        await worker.call_tool("update_status", {
                            "status": "working",
                            "task": "Building login form component",
                        })

                        # Worker sends message to Archie
                        await worker.call_tool("send_message", {
                            "to": "archie",
                            "content": "Login page is ready for review",
                        })

                        # Worker reports completion
                        await worker.call_tool("report_completion", {
                            "summary": "Login page implemented with validation",
                            "artifacts": ["src/Login.tsx", "src/Login.css"],
                        })

                # === Verify state updates ===
                agent = orch.state.get_agent(agent_id)
                assert agent["status"] == "done"

                # === Archie reads messages and tears down worker ===
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        # Archie reads messages
                        result = await archie.call_tool("get_messages", {})
                        msgs = json.loads(result.content[0].text)["messages"]
                        assert any("Login page is ready" in m["content"] for m in msgs), \
                            f"Archie should see worker's message. Got: {msgs}"

                        # Archie tears down the worker
                        result = await archie.call_tool("teardown_agent", {
                            "agent_id": agent_id,
                        })
                        teardown_data = json.loads(result.content[0].text)
                        assert teardown_data.get("ok") is True, \
                            f"Teardown should succeed: {teardown_data}"

                # === Verify cleanup ===
                assert not worktree_path.exists(), "Worktree should be removed after teardown"
                assert orch.state.get_agent(agent_id) is None, \
                    "Agent should be removed from state after teardown"

            finally:
                await orch.shutdown()

            assert orch._running is False

    @pytest.mark.asyncio
    async def test_spawn_creates_worktree_and_claude_md(self, e2e_config, e2e_repo):
        """Spawning via MCP creates real git worktree with CLAUDE.md."""
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        result = await archie.call_tool("spawn_agent", {
                            "role": "backend-dev",
                            "assignment": "Implement REST API for user management",
                        })
                        data = json.loads(result.content[0].text)
                        agent_id = data["agent_id"]
                        wt = Path(data["worktree_path"])

                # Verify git worktree is real
                assert wt.exists()
                git_result = subprocess.run(
                    ["git", "worktree", "list"],
                    cwd=e2e_repo,
                    capture_output=True, text=True,
                )
                assert agent_id in git_result.stdout, \
                    f"Agent worktree should appear in git worktree list: {git_result.stdout}"

                # Verify CLAUDE.md content
                claude_md = (wt / "CLAUDE.md").read_text()
                assert "REST API" in claude_md
                assert "user management" in claude_md
                assert "send_message" in claude_md  # Available tools listed

                # Verify state
                agent = orch.state.get_agent(agent_id)
                assert agent["role"] == "backend-dev"
                assert agent["worktree"] == str(wt)

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_worker_cannot_call_archie_tools(self, e2e_config, e2e_repo):
        """Worker calling spawn_agent through real MCP gets access denied."""
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Spawn a worker first
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()
                        result = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Test task",
                        })
                        agent_id = json.loads(result.content[0].text)["agent_id"]

                # Worker tries to call spawn_agent — should be denied
                async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                    async with ClientSession(r, w) as worker:
                        await worker.initialize()

                        result = await worker.call_tool("spawn_agent", {
                            "role": "backend-dev",
                            "assignment": "Rogue spawn attempt",
                        })
                        data = json.loads(result.content[0].text)
                        assert "error" in data
                        assert "Access denied" in data["error"]

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_multi_agent_messaging(self, e2e_config, e2e_repo):
        """Multiple agents exchange messages through real MCP server."""
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Spawn two workers
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        r1 = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Build UI components",
                        })
                        fe_id = json.loads(r1.content[0].text)["agent_id"]

                        r2 = await archie.call_tool("spawn_agent", {
                            "role": "backend-dev",
                            "assignment": "Build API endpoints",
                        })
                        be_id = json.loads(r2.content[0].text)["agent_id"]

                # Frontend sends message to backend
                async with sse_client(f"http://127.0.0.1:{port}/sse/{fe_id}") as (r, w):
                    async with ClientSession(r, w) as fe:
                        await fe.initialize()
                        await fe.call_tool("send_message", {
                            "to": be_id,
                            "content": "What endpoints are available?",
                        })

                # Backend reads messages and replies
                async with sse_client(f"http://127.0.0.1:{port}/sse/{be_id}") as (r, w):
                    async with ClientSession(r, w) as be:
                        await be.initialize()

                        result = await be.call_tool("get_messages", {})
                        msgs = json.loads(result.content[0].text)["messages"]
                        assert len(msgs) == 1
                        assert msgs[0]["from"] == fe_id
                        assert "endpoints" in msgs[0]["content"]

                        await be.call_tool("send_message", {
                            "to": fe_id,
                            "content": "GET /users and POST /users are ready",
                        })

                # Frontend reads backend's reply
                async with sse_client(f"http://127.0.0.1:{port}/sse/{fe_id}") as (r, w):
                    async with ClientSession(r, w) as fe:
                        await fe.initialize()

                        result = await fe.call_tool("get_messages", {})
                        msgs = json.loads(result.content[0].text)["messages"]
                        assert any("GET /users" in m["content"] for m in msgs)

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_merge_through_mcp(self, e2e_config, e2e_repo):
        """Spawn agent → commit in worktree → merge via MCP → verify on main."""
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(e2e_repo.parent)}

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Spawn worker
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        result = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Add login page",
                        })
                        data = json.loads(result.content[0].text)
                        agent_id = data["agent_id"]
                        wt = Path(data["worktree_path"])

                # Create a commit in the worker's worktree
                feature_file = wt / "login.py"
                feature_file.write_text("def login(user, pw): return True\n")
                subprocess.run(["git", "add", "login.py"], cwd=wt,
                                capture_output=True, check=True, env=env)
                subprocess.run(["git", "commit", "-m", "Add login feature"], cwd=wt,
                                capture_output=True, check=True, env=env)

                # Verify file doesn't exist on main yet
                assert not (e2e_repo / "login.py").exists()

                # Request merge via MCP
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()

                        result = await archie.call_tool("request_merge", {
                            "agent_id": agent_id,
                        })
                        merge_data = json.loads(result.content[0].text)
                        assert merge_data["status"] == "approved", \
                            f"Merge should succeed: {merge_data}"

                # Verify the file is now on main
                # (need to re-read from the repo root, not the worktree)
                subprocess.run(["git", "checkout", "main"], cwd=e2e_repo,
                                capture_output=True, env=env)
                assert (e2e_repo / "login.py").exists(), \
                    "Merged file should appear on main branch"

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_everything(self, e2e_config, e2e_repo):
        """After shutdown: worktrees removed, MCP server stopped, state persisted."""
        config_path, port = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            # Spawn two agents
            agent_ids = []
            async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                async with ClientSession(r, w) as archie:
                    await archie.initialize()

                    for role, task in [("frontend-dev", "Build UI"), ("backend-dev", "Build API")]:
                        result = await archie.call_tool("spawn_agent", {
                            "role": role,
                            "assignment": task,
                        })
                        data = json.loads(result.content[0].text)
                        agent_ids.append(data["agent_id"])

            # Verify agents exist before shutdown
            for aid in agent_ids:
                assert orch.state.get_agent(aid) is not None

            # Record worktree paths
            worktree_dir = e2e_repo / ".worktrees"
            assert worktree_dir.exists()

            # Shutdown
            await orch.shutdown()

            # Verify cleanup
            assert orch._running is False

            # Worktrees should be removed (default keep_worktrees=False)
            git_result = subprocess.run(
                ["git", "worktree", "list"], cwd=e2e_repo,
                capture_output=True, text=True,
            )
            for aid in agent_ids:
                assert aid not in git_result.stdout, \
                    f"Worktree for {aid} should be removed after shutdown"

            # MCP server should refuse connections
            import httpx
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/sse/test",
                                            timeout=1.0)
                    # If we get here, server is still up — that's a bug
                    pytest.fail("MCP server should be stopped after shutdown")
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass  # Expected — server is down
