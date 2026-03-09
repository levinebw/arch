"""
ARCH MCP Server

SSE/HTTP server providing MCP tools for agent coordination.
Agents connect via /sse/{agent_id} - the agent_id is extracted from the URL path.
Access controls enforce Archie-only tools vs worker tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Awaitable

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, Response
import uvicorn

from arch.state import StateStore, AGENT_STATUSES

logger = logging.getLogger(__name__)


# --- Tool Definitions ---

# Tools available to ALL agents
WORKER_TOOLS = [
    Tool(
        name="send_message",
        description="Send a message to another agent or to Archie",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "agent_id of recipient, 'archie', or 'broadcast'"
                },
                "content": {
                    "type": "string",
                    "description": "message body"
                }
            },
            "required": ["to", "content"]
        }
    ),
    Tool(
        name="get_messages",
        description="Retrieve messages addressed to you",
        inputSchema={
            "type": "object",
            "properties": {
                "since_id": {
                    "type": "string",
                    "description": "optional: only return messages newer than this ID"
                }
            }
        }
    ),
    Tool(
        name="update_status",
        description="Report your current task and status to the harness (shown in dashboard)",
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "what you are currently doing"
                },
                "status": {
                    "type": "string",
                    "enum": list(AGENT_STATUSES),
                    "description": "idle | working | blocked | waiting_review | done | error"
                }
            },
            "required": ["task", "status"]
        }
    ),
    Tool(
        name="report_completion",
        description="Signal that your assigned work is complete",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "what was accomplished"
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "list of files created or modified"
                }
            },
            "required": ["summary", "artifacts"]
        }
    ),
    Tool(
        name="save_progress",
        description="Persist structured session state for continuity across context compactions and restarts. Call periodically during long tasks and before signaling completion.",
        inputSchema={
            "type": "object",
            "properties": {
                "files_modified": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "files created or changed this session"
                },
                "progress": {
                    "type": "string",
                    "description": "summary of work completed so far"
                },
                "next_steps": {
                    "type": "string",
                    "description": "what remains to be done"
                },
                "blockers": {
                    "type": "string",
                    "description": "current blockers, if any"
                },
                "decisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "architectural/scope decisions made this session"
                }
            },
            "required": ["files_modified", "progress", "next_steps"]
        }
    ),
]

# System tools — called by Claude CLI permission system, not by agents directly.
# Not listed in any agent's tool catalog; dispatch handles them separately.
SYSTEM_TOOLS = [
    Tool(
        name="handle_permission_request",
        description="Handle permission prompt from Claude CLI. Called automatically when an agent requests a tool not in the pre-approved list. BLOCKS until user responds.",
        inputSchema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "name of the tool requesting permission"
                },
                "tool_args": {
                    "type": "string",
                    "description": "summary of arguments being passed to the tool"
                },
                "reason": {
                    "type": "string",
                    "description": "why you need to use this tool"
                }
            },
            "required": ["tool_name", "reason"]
        }
    ),
]

# Tools available ONLY to Archie
ARCHIE_ONLY_TOOLS = [
    Tool(
        name="spawn_agent",
        description="Spawn a new agent. Role must match an id from agent_pool (either configured in arch.yaml or added via plan_team).",
        inputSchema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "must match an id in agent_pool (from config or plan_team)"
                },
                "assignment": {
                    "type": "string",
                    "description": "task description given to agent at spawn"
                },
                "context": {
                    "type": "string",
                    "description": "optional additional context injected into agent's CLAUDE.md"
                },
                "skip_permissions": {
                    "type": "boolean",
                    "description": "request --dangerously-skip-permissions (requires config)"
                }
            },
            "required": ["role", "assignment"]
        }
    ),
    Tool(
        name="teardown_agent",
        description="Shut down an agent and remove its worktree",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "reason": {"type": "string"}
            },
            "required": ["agent_id"]
        }
    ),
    Tool(
        name="list_agents",
        description="Get current status of all active agents",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="escalate_to_user",
        description="Surface a question or decision to the human user. BLOCKS until answered.",
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "question shown in dashboard"
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "optional list of choices"
                }
            },
            "required": ["question"]
        }
    ),
    Tool(
        name="request_merge",
        description="Request merging an agent's worktree branch into target branch",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "whose worktree to merge"
                },
                "target_branch": {
                    "type": "string",
                    "description": "merge destination (default: main)"
                },
                "pr_title": {
                    "type": "string",
                    "description": "if provided, creates a GitHub PR instead of local merge"
                },
                "pr_body": {"type": "string"}
            },
            "required": ["agent_id"]
        }
    ),
    Tool(
        name="get_project_context",
        description="Get current project state: repo info, active agents, git status, and full BRIEF.md contents",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="close_project",
        description="Signal that the project work is complete. Initiates graceful shutdown.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"}
            },
            "required": ["summary"]
        }
    ),
    Tool(
        name="update_brief",
        description="Update a section of BRIEF.md. Use for Decisions Log entries and Current Status updates.",
        inputSchema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["current_status", "decisions_log"],
                    "description": "which section to update"
                },
                "content": {
                    "type": "string",
                    "description": "For current_status: full replacement text. For decisions_log: new row."
                }
            },
            "required": ["section", "content"]
        }
    ),
    Tool(
        name="list_personas",
        description="List all available agent personas from the project and system persona directories. Returns name, description, and file path for each.",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
    Tool(
        name="plan_team",
        description="Propose an agent team for the project. Analyzes the brief and selects personas. Requires user approval unless auto_approve_team is set. Must be called before spawn_agent if no agent_pool is configured.",
        inputSchema={
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "description": "List of agents to include in the team",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "description": "unique role id (e.g. 'frontend', 'qa', 'backend')"
                            },
                            "persona": {
                                "type": "string",
                                "description": "persona file path relative to project root (e.g. 'personas/frontend.md')"
                            },
                            "rationale": {
                                "type": "string",
                                "description": "why this role is needed for the project"
                            }
                        },
                        "required": ["role", "persona", "rationale"]
                    }
                },
                "summary": {
                    "type": "string",
                    "description": "brief summary of the team plan and how it maps to the project goals"
                }
            },
            "required": ["agents", "summary"]
        }
    ),
]

# GitHub tools (Archie only)
GITHUB_TOOLS = [
    Tool(
        name="gh_create_issue",
        description="Create a GitHub issue. Use for every discrete task assigned to an agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "milestone": {"type": "string"},
                "assignee": {"type": "string"}
            },
            "required": ["title", "body"]
        }
    ),
    Tool(
        name="gh_list_issues",
        description="List GitHub issues with optional filters.",
        inputSchema={
            "type": "object",
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "milestone": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"]
                },
                "limit": {"type": "integer"}
            }
        }
    ),
    Tool(
        name="gh_close_issue",
        description="Close a GitHub issue, optionally referencing the PR that resolves it.",
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "comment": {"type": "string"}
            },
            "required": ["issue_number"]
        }
    ),
    Tool(
        name="gh_update_issue",
        description="Update an issue's labels, milestone, or assignee.",
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "milestone": {"type": "string"},
                "assignee": {"type": "string"}
            },
            "required": ["issue_number"]
        }
    ),
    Tool(
        name="gh_add_comment",
        description="Add a comment to a GitHub issue.",
        inputSchema={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "body": {"type": "string"}
            },
            "required": ["issue_number", "body"]
        }
    ),
    Tool(
        name="gh_create_milestone",
        description="Create a GitHub milestone representing a sprint or phase.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "due_date": {"type": "string"}
            },
            "required": ["title"]
        }
    ),
    Tool(
        name="gh_list_milestones",
        description="List open GitHub milestones (sprints/phases).",
        inputSchema={
            "type": "object",
            "properties": {}
        }
    ),
]


class MCPServer:
    """
    ARCH MCP Server with SSE transport.

    Provides tools for agent coordination, message passing, and GitHub integration.
    Enforces access controls based on agent_id (Archie vs workers).
    """

    def __init__(
        self,
        state: StateStore,
        port: int = 3999,
        repo_path: Optional[Path] = None,
        github_repo: Optional[str] = None,
        on_spawn_agent: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
        on_teardown_agent: Optional[Callable[[str], Awaitable[bool]]] = None,
        on_request_merge: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
        on_close_project: Optional[Callable[[str], Awaitable[bool]]] = None,
        on_plan_team: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    ):
        """
        Initialize the MCP server.

        Args:
            state: StateStore instance for shared state.
            port: Port to listen on.
            repo_path: Path to git repository root.
            github_repo: GitHub repo in "owner/repo" format (enables GitHub tools).
            on_spawn_agent: Callback to spawn an agent (orchestrator handles this).
            on_teardown_agent: Callback to teardown an agent.
            on_request_merge: Callback to handle merge requests.
            on_close_project: Callback to handle project close.
        """
        self.state = state
        self.port = port
        self.repo_path = Path(repo_path) if repo_path else None
        self.github_repo = github_repo

        # Callbacks for orchestrator actions
        self.on_spawn_agent = on_spawn_agent
        self.on_teardown_agent = on_teardown_agent
        self.on_request_merge = on_request_merge
        self.on_close_project = on_close_project
        self.on_plan_team = on_plan_team

        # Pending escalations: decision_id -> asyncio.Event
        self._pending_escalations: dict[str, asyncio.Event] = {}

        # Runtime permission allowlist: agent_id -> set of tool patterns
        # Session-scoped: populated when user chooses "always" for a permission
        self._runtime_allowed: dict[str, set[str]] = {}

        # Persistent MCP server instances per agent
        self._mcp_servers: dict[str, Server] = {}

        # Active SSE transports per agent (for routing POST messages)
        self._active_transports: dict[str, SseServerTransport] = {}

        # Event log (JSONL file in state directory)
        self._event_log_path: Optional[Path] = None
        if self.state.state_dir:
            self._event_log_path = Path(self.state.state_dir) / "events.jsonl"

        # Server state
        self._server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None

        # Build tool lists
        self._worker_tool_names = {t.name for t in WORKER_TOOLS}
        self._archie_tool_names = self._worker_tool_names | {t.name for t in ARCHIE_ONLY_TOOLS}
        self._system_tool_names = {t.name for t in SYSTEM_TOOLS}

        if self.github_repo:
            self._archie_tool_names |= {t.name for t in GITHUB_TOOLS}

    def _is_archie(self, agent_id: str) -> bool:
        """Check if agent_id is Archie."""
        return agent_id == "archie"

    def _get_tools_for_agent(self, agent_id: str) -> list[Tool]:
        """Get the list of tools available to an agent.

        SYSTEM_TOOLS (e.g., handle_permission_request) are included in the
        catalog so Claude CLI can discover them for --permission-prompt-tool.
        """
        if self._is_archie(agent_id):
            tools = WORKER_TOOLS + ARCHIE_ONLY_TOOLS + SYSTEM_TOOLS
            if self.github_repo:
                tools = tools + GITHUB_TOOLS
            return tools
        return WORKER_TOOLS + SYSTEM_TOOLS

    def _check_tool_access(self, agent_id: str, tool_name: str) -> bool:
        """Check if an agent has access to a tool.

        System tools (e.g., handle_permission_request) are callable by any agent
        since they are invoked by Claude CLI's permission system, not by agents directly.
        """
        if tool_name in self._system_tool_names:
            return True
        if self._is_archie(agent_id):
            return tool_name in self._archie_tool_names
        return tool_name in self._worker_tool_names

    # --- Tool Implementations ---

    async def _handle_send_message(
        self,
        agent_id: str,
        to: str,
        content: str
    ) -> dict[str, Any]:
        """Handle send_message tool."""
        message = self.state.add_message(agent_id, to, content)
        return {
            "message_id": message["id"],
            "timestamp": message["timestamp"]
        }

    async def _handle_get_messages(
        self,
        agent_id: str,
        since_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle get_messages tool."""
        messages, cursor = self.state.get_messages(agent_id, since_id)
        return {
            "messages": messages,
            "cursor": cursor
        }

    async def _handle_update_status(
        self,
        agent_id: str,
        task: str,
        status: str
    ) -> dict[str, Any]:
        """Handle update_status tool."""
        result = self.state.update_agent(agent_id, task=task, status=status)
        return {"ok": result is not None}

    async def _handle_report_completion(
        self,
        agent_id: str,
        summary: str,
        artifacts: list[str]
    ) -> dict[str, Any]:
        """Handle report_completion tool."""
        # Update agent status
        self.state.update_agent(agent_id, status="done", task=summary)

        # Send completion message to Archie
        self.state.add_message(
            agent_id,
            "archie",
            f"Work complete: {summary}\nArtifacts: {', '.join(artifacts)}"
        )

        return {"ok": True}

    async def _handle_save_progress(
        self,
        agent_id: str,
        files_modified: list[str],
        progress: str,
        next_steps: str,
        blockers: Optional[str] = None,
        decisions: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """
        Handle save_progress tool.

        Persists structured session state to the agent's context field.
        On agent restart, this context is injected into CLAUDE.md as a
        "## Session State" section for continuity.
        """
        context = {
            "files_modified": files_modified,
            "progress": progress,
            "next_steps": next_steps,
            "blockers": blockers,
            "decisions": decisions or [],
        }

        result = self.state.update_agent(agent_id, context=context)
        return {"ok": result is not None}

    async def _handle_spawn_agent(
        self,
        role: str,
        assignment: str,
        context: Optional[str] = None,
        skip_permissions: bool = False
    ) -> dict[str, Any]:
        """Handle spawn_agent tool (Archie only)."""
        if self.on_spawn_agent is None:
            return {"error": "spawn_agent callback not configured"}

        result = await self.on_spawn_agent(
            role=role,
            assignment=assignment,
            context=context,
            skip_permissions=skip_permissions
        )
        return result

    async def _handle_teardown_agent(
        self,
        agent_id: str,
        reason: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle teardown_agent tool (Archie only)."""
        if self.on_teardown_agent is None:
            return {"error": "teardown_agent callback not configured"}

        # Notify the agent first
        if reason:
            self.state.add_message("archie", agent_id, f"Shutting down: {reason}")

        result = await self.on_teardown_agent(agent_id)
        return {"ok": result}

    async def _handle_list_agents(self) -> dict[str, Any]:
        """Handle list_agents tool (Archie only)."""
        agents = self.state.list_agents()
        return {
            "agents": [
                {
                    "id": a["id"],
                    "role": a["role"],
                    "status": a["status"],
                    "task": a["task"],
                    "tokens_used": a["usage"].get("input_tokens", 0) + a["usage"].get("output_tokens", 0),
                    "cost_usd": a["usage"].get("cost_usd", 0.0)
                }
                for a in agents
            ]
        }

    async def _escalate_and_wait(
        self,
        question: str,
        options: Optional[list[str]] = None
    ) -> str:
        """
        Create a pending decision and block until user answers.

        Returns the user's answer string.
        """
        decision = self.state.add_pending_decision(question, options)
        decision_id = decision["id"]

        event = asyncio.Event()
        self._pending_escalations[decision_id] = event

        logger.info(f"Escalation {decision_id}: waiting for user answer")

        await event.wait()

        del self._pending_escalations[decision_id]

        decisions = [
            d for d in self.state._state["pending_user_decisions"]
            if d["id"] == decision_id
        ]

        if decisions and decisions[0]["answer"]:
            return decisions[0]["answer"]
        return ""

    async def _handle_escalate_to_user(
        self,
        question: str,
        options: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """
        Handle escalate_to_user tool (Archie only).

        BLOCKS until user answers via the dashboard.
        """
        answer = await self._escalate_and_wait(question, options)
        if answer:
            return {"answer": answer}
        return {"answer": "", "error": "No answer received"}

    def answer_escalation(self, decision_id: str, answer: str) -> bool:
        """
        Answer a pending escalation (called by dashboard).

        Returns True if the escalation was found and answered.
        """
        if self.state.answer_decision(decision_id, answer):
            # Signal the waiting coroutine
            if decision_id in self._pending_escalations:
                self._pending_escalations[decision_id].set()
                return True
        return False

    def _check_runtime_allowed(self, agent_id: str, tool_name: str) -> bool:
        """
        Check if a tool is in the runtime allowlist for an agent.

        Returns True if the tool was previously approved with "always".
        """
        if agent_id not in self._runtime_allowed:
            return False
        return tool_name in self._runtime_allowed[agent_id]

    def add_runtime_allowed(self, agent_id: str, tool_name: str) -> None:
        """
        Add a tool to the runtime allowlist for an agent.

        Called when user chooses "always" for a permission request.
        """
        if agent_id not in self._runtime_allowed:
            self._runtime_allowed[agent_id] = set()
        self._runtime_allowed[agent_id].add(tool_name)
        logger.info(f"Added {tool_name} to runtime allowlist for {agent_id}")

    async def _handle_permission_request(
        self,
        agent_id: str,
        tool_name: str,
        tool_args: Optional[str] = None,
        reason: str = ""
    ) -> dict[str, Any]:
        """
        Handle permission request for a tool not in the pre-approved list.

        BLOCKS until user answers via the dashboard.
        Returns:
            - {"approved": True} if permission granted
            - {"approved": False, "reason": "..."} if denied
        """
        # Check if already approved via "always"
        if self._check_runtime_allowed(agent_id, tool_name):
            logger.debug(f"Permission for {tool_name} auto-approved for {agent_id}")
            return {"approved": True, "source": "runtime_allowlist"}

        # Build the question for the user
        question = f"Agent '{agent_id}' requests permission to use tool: {tool_name}"
        if tool_args:
            question += f"\nArguments: {tool_args}"
        if reason:
            question += f"\nReason: {reason}"

        # Options: [y]once, [a]lways, [n]o
        options = ["yes (this time)", "always (this session)", "no"]

        # Create pending decision
        decision = self.state.add_pending_decision(question, options)
        decision_id = decision["id"]

        # Tag the decision as a permission request for the dashboard
        # (Store metadata in the decision for the dashboard to recognize)
        self.state._state["pending_user_decisions"][-1]["type"] = "permission_request"
        self.state._state["pending_user_decisions"][-1]["agent_id"] = agent_id
        self.state._state["pending_user_decisions"][-1]["tool_name"] = tool_name

        # Create event for blocking
        event = asyncio.Event()
        self._pending_escalations[decision_id] = event

        logger.info(f"Permission request {decision_id}: {agent_id} wants {tool_name}")

        # Block until answered
        await event.wait()

        # Clean up
        del self._pending_escalations[decision_id]

        # Get the answer
        decisions = [
            d for d in self.state._state["pending_user_decisions"]
            if d["id"] == decision_id
        ]

        if not decisions or not decisions[0].get("answer"):
            return {"approved": False, "reason": "No answer received"}

        answer = decisions[0]["answer"].lower()

        # Handle the response
        if answer.startswith("yes"):
            return {"approved": True, "source": "user_once"}
        elif answer.startswith("always"):
            # Add to runtime allowlist
            self.add_runtime_allowed(agent_id, tool_name)
            return {"approved": True, "source": "user_always"}
        else:
            return {"approved": False, "reason": "User denied permission"}

    async def _handle_request_merge(
        self,
        agent_id: str,
        target_branch: str = "main",
        pr_title: Optional[str] = None,
        pr_body: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle request_merge tool (Archie only)."""
        if self.on_request_merge is None:
            return {"error": "request_merge callback not configured"}

        result = await self.on_request_merge(
            agent_id=agent_id,
            target_branch=target_branch,
            pr_title=pr_title,
            pr_body=pr_body
        )
        return result

    async def _handle_get_project_context(self) -> dict[str, Any]:
        """Handle get_project_context tool (Archie only)."""
        project = self.state.get_project()
        agents = self.state.list_agents()

        # Read BRIEF.md if available
        brief_content = ""
        if self.repo_path:
            brief_path = self.repo_path / "BRIEF.md"
            if brief_path.exists():
                brief_content = brief_path.read_text()

        # Get git status
        git_status = ""
        if self.repo_path:
            try:
                result = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                git_status = result.stdout
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                git_status = "(git status unavailable)"

        # List open worktrees
        open_worktrees = []
        if self.repo_path:
            worktree_dir = self.repo_path / ".worktrees"
            if worktree_dir.exists():
                open_worktrees = [d.name for d in worktree_dir.iterdir() if d.is_dir()]

        return {
            "name": project.get("name", ""),
            "description": project.get("description", ""),
            "repo_path": str(self.repo_path) if self.repo_path else "",
            "active_agents": [{"id": a["id"], "role": a["role"], "status": a["status"]} for a in agents],
            "git_status": git_status,
            "open_worktrees": open_worktrees,
            "brief": brief_content
        }

    async def _handle_close_project(self, summary: str) -> dict[str, Any]:
        """Handle close_project tool (Archie only)."""
        if self.on_close_project is None:
            return {"error": "close_project callback not configured"}

        # Auto-update BRIEF.md current status with the close summary
        await self._handle_update_brief(
            section="current_status",
            content=f"COMPLETE — {summary}"
        )

        # Escalate to user for confirmation before shutting down
        answer = await self._escalate_and_wait(
            question=f"Archie wants to close the project:\n\n{summary}\n\nIs everything done?",
            options=["Yes, shut down", "No, keep working"],
        )

        if answer and "no" in answer.lower() or "keep" in answer.lower():
            # User wants to keep working — notify Archie
            self.state.add_message(
                from_agent="system",
                to_agent="archie",
                content=f"User declined project close: \"{answer}\". Continue working."
            )
            return {"ok": False, "reason": f"User declined: {answer}"}

        # Notify Archie to review the brief
        self.state.add_message(
            from_agent="system",
            to_agent="archie",
            content="BRIEF.md has been auto-updated with the project summary. "
                    "Please review and finalize BRIEF.md — update the Current Status "
                    "and Decisions Log sections with any remaining details before shutdown."
        )

        result = await self.on_close_project(summary)
        return {"ok": result}

    async def _handle_update_brief(
        self,
        section: str,
        content: str
    ) -> dict[str, Any]:
        """Handle update_brief tool (Archie only)."""
        if not self.repo_path:
            return {"ok": False, "error": "repo_path not configured"}

        brief_path = self.repo_path / "BRIEF.md"

        if not brief_path.exists():
            return {"ok": False, "error": "BRIEF.md not found"}

        try:
            brief_content = brief_path.read_text()

            if section == "current_status":
                # Replace the Current Status section
                import re
                pattern = r"(## Current Status\n).*?(?=\n## |\Z)"
                replacement = f"\\1{content}\n"
                new_content = re.sub(pattern, replacement, brief_content, flags=re.DOTALL)

            elif section == "decisions_log":
                # Append to Decisions Log table
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                new_row = f"| {today} | {content} |"

                # Find the table and append
                lines = brief_content.split("\n")
                new_lines = []
                in_decisions = False

                for line in lines:
                    new_lines.append(line)
                    if "## Decisions Log" in line:
                        in_decisions = True
                    elif in_decisions and line.startswith("|") and "---" in line:
                        # After the header separator, append the new row
                        new_lines.append(new_row)
                        in_decisions = False

                new_content = "\n".join(new_lines)

            else:
                return {"ok": False, "error": f"Unknown section: {section}"}

            brief_path.write_text(new_content)
            return {"ok": True}

        except Exception as e:
            logger.error(f"Failed to update BRIEF.md: {e}")
            return {"ok": False, "error": str(e)}

    # --- Team Planning Tools ---

    def _scan_persona_dirs(self) -> list[dict[str, str]]:
        """Scan persona directories and return available personas."""
        personas = []
        seen_names = set()

        # Directories to scan (project-local first, then system)
        dirs_to_scan = []
        if self.repo_path:
            dirs_to_scan.append(self.repo_path / "personas")
            dirs_to_scan.append(self.repo_path / "agents")

        # System personas from ARCH install directory
        arch_dir = Path(__file__).parent.parent / "personas"
        dirs_to_scan.append(arch_dir)

        for persona_dir in dirs_to_scan:
            if not persona_dir.is_dir():
                continue
            for md_file in sorted(persona_dir.glob("*.md")):
                name = md_file.stem
                if name == "archie" or name in seen_names:
                    continue
                seen_names.add(name)

                # Extract title and description from first lines
                title = name
                description = ""
                try:
                    with open(md_file) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("# "):
                                title = line[2:].strip()
                            elif line and not line.startswith("#"):
                                description = line
                                break
                except Exception:
                    pass

                # Compute path relative to repo or absolute
                if self.repo_path and md_file.is_relative_to(self.repo_path):
                    rel_path = str(md_file.relative_to(self.repo_path))
                else:
                    rel_path = str(md_file)

                personas.append({
                    "name": name,
                    "title": title,
                    "description": description,
                    "path": rel_path,
                })

        return personas

    async def _handle_list_personas(self) -> dict[str, Any]:
        """Handle list_personas tool (Archie only)."""
        personas = self._scan_persona_dirs()
        return {"personas": personas, "count": len(personas)}

    async def _handle_plan_team(
        self,
        agents: list[dict[str, str]],
        summary: str
    ) -> dict[str, Any]:
        """Handle plan_team tool (Archie only).

        Validates the proposed team, then either auto-approves or
        escalates to the user for approval.
        """
        if not self.on_plan_team:
            return {"error": "plan_team callback not configured"}

        # Validate personas exist
        available = {p["name"]: p for p in self._scan_persona_dirs()}
        for agent in agents:
            persona_path = agent.get("persona", "")
            persona_name = Path(persona_path).stem
            if persona_name not in available:
                return {
                    "error": f"Unknown persona '{persona_path}'. "
                             f"Available: {list(available.keys())}"
                }

        return await self.on_plan_team(agents, summary)

    # --- GitHub Tool Implementations ---

    async def _handle_gh_create_issue(
        self,
        title: str,
        body: str,
        labels: Optional[list[str]] = None,
        milestone: Optional[str] = None,
        assignee: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle gh_create_issue tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = ["gh", "issue", "create", "--repo", self.github_repo, "--title", title, "--body", body]

        if labels:
            cmd.extend(["--label", ",".join(labels)])
        if milestone:
            cmd.extend(["--milestone", milestone])
        if assignee:
            cmd.extend(["--assignee", assignee])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {"error": result.stderr}

            # Parse the URL to extract issue number
            url = result.stdout.strip()
            issue_number = int(url.split("/")[-1]) if url else 0

            return {"issue_number": issue_number, "url": url}

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_list_issues(
        self,
        labels: Optional[list[str]] = None,
        milestone: Optional[str] = None,
        state: str = "open",
        limit: int = 30
    ) -> dict[str, Any]:
        """Handle gh_list_issues tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = [
            "gh", "issue", "list", "--repo", self.github_repo,
            "--json", "number,title,labels,state,assignees,url",
            "--state", state,
            "--limit", str(limit)
        ]

        if labels:
            for label in labels:
                cmd.extend(["--label", label])
        if milestone:
            cmd.extend(["--milestone", milestone])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {"error": result.stderr}

            issues = json.loads(result.stdout) if result.stdout else []

            # Transform to match spec format
            return {
                "issues": [
                    {
                        "number": i["number"],
                        "title": i["title"],
                        "labels": [l["name"] for l in i.get("labels", [])],
                        "state": i["state"],
                        "assignee": i.get("assignees", [{}])[0].get("login") if i.get("assignees") else None,
                        "url": i["url"]
                    }
                    for i in issues
                ]
            }

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_close_issue(
        self,
        issue_number: int,
        comment: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle gh_close_issue tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = ["gh", "issue", "close", str(issue_number), "--repo", self.github_repo]

        if comment:
            cmd.extend(["--comment", comment])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"ok": result.returncode == 0, "error": result.stderr if result.returncode != 0 else None}

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_update_issue(
        self,
        issue_number: int,
        add_labels: Optional[list[str]] = None,
        remove_labels: Optional[list[str]] = None,
        milestone: Optional[str] = None,
        assignee: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle gh_update_issue tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = ["gh", "issue", "edit", str(issue_number), "--repo", self.github_repo]

        if add_labels:
            cmd.extend(["--add-label", ",".join(add_labels)])
        if remove_labels:
            cmd.extend(["--remove-label", ",".join(remove_labels)])
        if milestone:
            cmd.extend(["--milestone", milestone])
        if assignee:
            cmd.extend(["--add-assignee", assignee])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"ok": result.returncode == 0, "error": result.stderr if result.returncode != 0 else None}

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_add_comment(
        self,
        issue_number: int,
        body: str
    ) -> dict[str, Any]:
        """Handle gh_add_comment tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = ["gh", "issue", "comment", str(issue_number), "--repo", self.github_repo, "--body", body]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {"ok": result.returncode == 0, "error": result.stderr if result.returncode != 0 else None}

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_create_milestone(
        self,
        title: str,
        description: Optional[str] = None,
        due_date: Optional[str] = None
    ) -> dict[str, Any]:
        """Handle gh_create_milestone tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        # gh CLI doesn't have direct milestone create, use API
        cmd = [
            "gh", "api", f"repos/{self.github_repo}/milestones",
            "-X", "POST",
            "-f", f"title={title}"
        ]

        if description:
            cmd.extend(["-f", f"description={description}"])
        if due_date:
            cmd.extend(["-f", f"due_on={due_date}T00:00:00Z"])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {"error": result.stderr}

            data = json.loads(result.stdout) if result.stdout else {}
            return {
                "milestone_number": data.get("number"),
                "url": data.get("html_url", "")
            }

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_gh_list_milestones(self) -> dict[str, Any]:
        """Handle gh_list_milestones tool."""
        if not self.github_repo:
            return {"error": "GitHub not configured"}

        cmd = [
            "gh", "api", f"repos/{self.github_repo}/milestones",
            "--jq", ".[].{number, title, open_issues, closed_issues, due_on, html_url}"
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {"error": result.stderr}

            # Parse JSONL output
            milestones = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    m = json.loads(line)
                    milestones.append({
                        "number": m.get("number"),
                        "title": m.get("title"),
                        "open_issues": m.get("open_issues"),
                        "closed_issues": m.get("closed_issues"),
                        "due_date": m.get("due_on"),
                        "url": m.get("html_url", "")
                    })

            return {"milestones": milestones}

        except subprocess.TimeoutExpired:
            return {"error": "Command timed out"}
        except Exception as e:
            return {"error": str(e)}

    # --- Tool Dispatch ---

    def _log_event(
        self,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        duration_ms: float,
    ) -> None:
        """Append tool call event to events.jsonl."""
        if not self._event_log_path:
            return

        # Summarize result — keep it compact
        if isinstance(result, dict):
            if "error" in result:
                status = "error"
                detail = result["error"]
            else:
                status = "ok"
                detail = {k: v for k, v in result.items()
                          if k in ("agent_id", "ok", "status", "count", "merged")}
            result_summary = {"status": status, **detail} if isinstance(detail, dict) else {"status": status, "detail": detail}
        else:
            result_summary = {"status": "ok"}

        # Summarize large arguments (truncate message content, etc.)
        args_summary = {}
        for k, v in arguments.items():
            if isinstance(v, str) and len(v) > 200:
                args_summary[k] = v[:200] + "..."
            else:
                args_summary[k] = v

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary,
            "duration_ms": round(duration_ms, 1),
        }

        try:
            self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._event_log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.debug(f"Failed to write event log: {e}")

    async def _handle_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any]
    ) -> Any:
        """Dispatch a tool call to the appropriate handler."""
        # Check access
        if not self._check_tool_access(agent_id, tool_name):
            return {"error": f"Access denied: {tool_name} is not available to {agent_id}"}

        start = time.monotonic()

        # Worker tools
        if tool_name == "send_message":
            result = await self._handle_send_message(agent_id, **arguments)
        elif tool_name == "get_messages":
            result = await self._handle_get_messages(agent_id, **arguments)
        elif tool_name == "update_status":
            result = await self._handle_update_status(agent_id, **arguments)
        elif tool_name == "report_completion":
            result = await self._handle_report_completion(agent_id, **arguments)
        elif tool_name == "save_progress":
            result = await self._handle_save_progress(agent_id, **arguments)
        elif tool_name == "handle_permission_request":
            result = await self._handle_permission_request(agent_id=agent_id, **arguments)

        # Archie-only tools
        elif tool_name == "spawn_agent":
            result = await self._handle_spawn_agent(**arguments)
        elif tool_name == "teardown_agent":
            result = await self._handle_teardown_agent(**arguments)
        elif tool_name == "list_agents":
            result = await self._handle_list_agents()
        elif tool_name == "escalate_to_user":
            result = await self._handle_escalate_to_user(**arguments)
        elif tool_name == "request_merge":
            result = await self._handle_request_merge(**arguments)
        elif tool_name == "get_project_context":
            result = await self._handle_get_project_context()
        elif tool_name == "close_project":
            result = await self._handle_close_project(**arguments)
        elif tool_name == "update_brief":
            result = await self._handle_update_brief(**arguments)
        elif tool_name == "list_personas":
            result = await self._handle_list_personas()
        elif tool_name == "plan_team":
            result = await self._handle_plan_team(**arguments)

        # GitHub tools
        elif tool_name == "gh_create_issue":
            result = await self._handle_gh_create_issue(**arguments)
        elif tool_name == "gh_list_issues":
            result = await self._handle_gh_list_issues(**arguments)
        elif tool_name == "gh_close_issue":
            result = await self._handle_gh_close_issue(**arguments)
        elif tool_name == "gh_update_issue":
            result = await self._handle_gh_update_issue(**arguments)
        elif tool_name == "gh_add_comment":
            result = await self._handle_gh_add_comment(**arguments)
        elif tool_name == "gh_create_milestone":
            result = await self._handle_gh_create_milestone(**arguments)
        elif tool_name == "gh_list_milestones":
            result = await self._handle_gh_list_milestones()

        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        elapsed = (time.monotonic() - start) * 1000
        self._log_event(agent_id, tool_name, arguments, result, elapsed)
        return result

    # --- Server Setup ---

    def get_or_create_mcp_server(self, agent_id: str) -> Server:
        """
        Get or create an MCP Server instance for a specific agent.

        Server instances are cached to ensure consistent state across
        SSE and POST handlers.
        """
        if agent_id not in self._mcp_servers:
            server = Server(f"arch-{agent_id}")

            @server.list_tools()
            async def list_tools() -> list[Tool]:
                return self._get_tools_for_agent(agent_id)

            @server.call_tool()
            async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
                result = await self._handle_tool_call(agent_id, name, arguments)
                return [TextContent(type="text", text=json.dumps(result))]

            self._mcp_servers[agent_id] = server

        return self._mcp_servers[agent_id]

    def create_mcp_server(self, agent_id: str) -> Server:
        """Create an MCP Server instance for a specific agent (for testing)."""
        return self.get_or_create_mcp_server(agent_id)

    def create_app(self) -> Starlette:
        """Create the Starlette ASGI application with SSE endpoints."""
        # Reference to self for use in closures
        mcp_server = self

        async def handle_sse(request):
            """Handle SSE connection for an agent."""
            agent_id = request.path_params.get("agent_id", "unknown")
            logger.info(f"SSE connection from agent: {agent_id}")

            # Get or create persistent MCP server for this agent
            server = mcp_server.get_or_create_mcp_server(agent_id)

            # Create SSE transport with path that includes agent_id
            transport = SseServerTransport(f"/messages/{agent_id}")

            # Store transport for POST handler to use
            mcp_server._active_transports[agent_id] = transport

            try:
                # Handle the SSE connection — transport owns the ASGI response
                async with transport.connect_sse(
                    request.scope,
                    request.receive,
                    request._send
                ) as (read_stream, write_stream):
                    await server.run(
                        read_stream,
                        write_stream,
                        server.create_initialization_options()
                    )
            except Exception:
                # SSE transport already consumed the connection; errors on
                # disconnect are expected and harmless.
                pass
            finally:
                # Clean up transport when connection closes
                mcp_server._active_transports.pop(agent_id, None)

            return Response()

        async def handle_messages(request):
            """Handle POST messages for SSE transport."""
            agent_id = request.path_params.get("agent_id", "unknown")

            # Get the active transport for this agent
            transport = mcp_server._active_transports.get(agent_id)

            if transport is None:
                logger.warning(f"POST message for disconnected agent: {agent_id}")
                return Response(status_code=404, content="Agent not connected")

            # Route the message through the transport's POST handler
            return await transport.handle_post_message(
                request.scope,
                request.receive,
                request._send
            )

        async def handle_escalation_answer(request):
            """Handle POST to answer a pending escalation/permission request."""
            decision_id = request.path_params.get("decision_id", "")

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

            answer = body.get("answer")
            if not answer:
                return JSONResponse({"ok": False, "error": "Missing answer"}, status_code=400)

            success = mcp_server.answer_escalation(decision_id, answer)
            if success:
                return JSONResponse({"ok": True})
            else:
                return JSONResponse(
                    {"ok": False, "error": "Decision not found or already answered"},
                    status_code=404,
                )

        async def handle_health(request):
            """Health check endpoint for dashboard connectivity."""
            return JSONResponse({"status": "running", "port": mcp_server.port})

        routes = [
            Route("/sse/{agent_id}", handle_sse),
            Route("/messages/{agent_id}", handle_messages, methods=["POST"]),
            Route("/api/escalation/{decision_id}", handle_escalation_answer, methods=["POST"]),
            Route("/api/health", handle_health),
        ]

        starlette_app = Starlette(routes=routes)

        # Wrap to suppress TypeError from SSE disconnect. When a client
        # disconnects, the SSE transport has already consumed the ASGI send
        # callable, so Starlette's attempt to send the Response raises
        # TypeError. This is harmless and expected.
        async def app(scope, receive, send):
            try:
                await starlette_app(scope, receive, send)
            except TypeError:
                pass

        return app

    async def start(self, background: bool = True):
        """
        Start the MCP server.

        Args:
            background: If True, run server in a background task (non-blocking).
                       If False, run blocking until server stops.
        """
        app = self.create_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)

        if background:
            self._server_task = asyncio.create_task(self._server.serve())
            # Give server time to start
            await asyncio.sleep(0.1)
            logger.info(f"MCP server started on port {self.port}")
        else:
            await self._server.serve()

    async def stop(self):
        """Stop the MCP server."""
        if self._server:
            self._server.should_exit = True

            if self._server_task:
                try:
                    await asyncio.wait_for(self._server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("MCP server shutdown timed out")
                    self._server_task.cancel()
                    try:
                        await self._server_task
                    except asyncio.CancelledError:
                        pass

            self._server = None
            self._server_task = None
            logger.info("MCP server stopped")

    def run(self):
        """Run the MCP server (blocking)."""
        asyncio.run(self.start(background=False))
