"""
ARCH State Store

Single source of truth for all runtime state. In-memory Python dict with
automatic JSON persistence to state/*.json after every mutation.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Type aliases for clarity
AgentId = str
MessageId = str
TaskId = str
DecisionId = str

# Valid enum values
AGENT_STATUSES = frozenset({"idle", "working", "blocked", "waiting_review", "done", "error"})
TASK_STATUSES = frozenset({"pending", "in_progress", "done"})


class InvalidStatusError(ValueError):
    """Raised when an invalid status value is provided."""
    pass


def validate_agent_status(status: str) -> str:
    """Validate and return agent status, or raise InvalidStatusError."""
    if status not in AGENT_STATUSES:
        raise InvalidStatusError(
            f"Invalid agent status '{status}'. Must be one of: {', '.join(sorted(AGENT_STATUSES))}"
        )
    return status


def validate_task_status(status: str) -> str:
    """Validate and return task status, or raise InvalidStatusError."""
    if status not in TASK_STATUSES:
        raise InvalidStatusError(
            f"Invalid task status '{status}'. Must be one of: {', '.join(sorted(TASK_STATUSES))}"
        )
    return status


def utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def generate_id() -> str:
    """Generate a unique ID."""
    return str(uuid.uuid4())[:8]


class StateStore:
    """
    Thread-safe state store with automatic JSON persistence.

    All state is held in memory and flushed to JSON files after every mutation.
    The store supports loading existing state from disk on initialization.
    """

    def __init__(self, state_dir: str | Path):
        """
        Initialize the state store.

        Args:
            state_dir: Directory where state JSON files are stored.
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()

        # Initialize state structure
        self._state: dict[str, Any] = {
            "project": {
                "name": "",
                "description": "",
                "repo": "",
                "started_at": ""
            },
            "agents": {},
            "messages": [],
            "pending_user_decisions": [],
            "tasks": []
        }

        # Cursors for message read tracking (persisted separately)
        self._cursors: dict[str, str] = {}

        # Load existing state if present
        self._load()

    # --- Project Operations ---

    def init_project(self, name: str, description: str, repo: str) -> None:
        """Initialize project metadata."""
        with self._lock:
            self._state["project"] = {
                "name": name,
                "description": description,
                "repo": repo,
                "started_at": utc_now()
            }
            self._flush()

    def update_project(self, **kwargs: Any) -> None:
        """Update project metadata fields."""
        with self._lock:
            self._state["project"].update(kwargs)
            self._flush()

    def get_project(self) -> dict[str, str]:
        """Get project metadata."""
        with self._lock:
            return dict(self._state["project"])

    # --- Agent Operations ---

    def register_agent(
        self,
        agent_id: str,
        role: str,
        worktree: str,
        sandboxed: bool = False,
        skip_permissions: bool = False,
        pid: Optional[int] = None,
        container_name: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Register a new agent.

        Args:
            agent_id: Unique agent identifier
            role: Agent role from agent_pool config
            worktree: Path to agent's git worktree
            sandboxed: Whether running in Docker container
            skip_permissions: Whether running with --dangerously-skip-permissions
            pid: Local process ID (None if containerized)
            container_name: Docker container name (None if local)

        Returns:
            The created agent record.
        """
        with self._lock:
            agent = {
                "id": agent_id,
                "role": role,
                "status": "idle",
                "task": "",
                "session_id": None,
                "worktree": worktree,
                "pid": pid,
                "container_name": container_name,
                "sandboxed": sandboxed,
                "skip_permissions": skip_permissions,
                "spawned_at": utc_now(),
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "turns": 0,
                    "cost_usd": 0.0
                }
            }
            self._state["agents"][agent_id] = agent
            self._flush()
            return dict(agent)

    def get_agent(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Get agent by ID. Returns None if not found."""
        with self._lock:
            agent = self._state["agents"].get(agent_id)
            return dict(agent) if agent else None

    def list_agents(self) -> list[dict[str, Any]]:
        """List all agents."""
        with self._lock:
            return [dict(a) for a in self._state["agents"].values()]

    def update_agent(self, agent_id: str, **updates: Any) -> Optional[dict[str, Any]]:
        """
        Update an agent's fields.

        Args:
            agent_id: Agent to update
            **updates: Fields to update (status, task, session_id, pid, context, etc.)
                       The `context` field stores structured session state for continuity
                       across restarts. See save_progress MCP tool.

        Returns:
            Updated agent record, or None if not found.

        Raises:
            InvalidStatusError: If status value is invalid.
        """
        # Validate status before acquiring lock
        if "status" in updates:
            validate_agent_status(updates["status"])

        with self._lock:
            if agent_id not in self._state["agents"]:
                return None

            agent = self._state["agents"][agent_id]

            # Handle nested usage updates
            if "usage" in updates:
                agent["usage"].update(updates.pop("usage"))

            # Handle nested context updates (merge rather than replace)
            if "context" in updates:
                if "context" not in agent or agent["context"] is None:
                    agent["context"] = {}
                agent["context"].update(updates.pop("context"))

            agent.update(updates)
            self._flush()
            return dict(agent)

    def remove_agent(self, agent_id: str) -> bool:
        """
        Remove an agent.

        Returns:
            True if agent was removed, False if not found.
        """
        with self._lock:
            if agent_id in self._state["agents"]:
                del self._state["agents"][agent_id]
                self._flush()
                return True
            return False

    # --- Message Operations ---

    def add_message(
        self,
        from_agent: str,
        to_agent: str,
        content: str
    ) -> dict[str, Any]:
        """
        Add a message to the message bus.

        Args:
            from_agent: Sender agent_id
            to_agent: Recipient agent_id, "archie", or "broadcast"
            content: Message body

        Returns:
            The created message record.
        """
        with self._lock:
            message = {
                "id": generate_id(),
                "from": from_agent,
                "to": to_agent,
                "content": content,
                "timestamp": utc_now(),
                "read": False
            }
            self._state["messages"].append(message)
            self._flush()
            return dict(message)

    def get_messages(
        self,
        for_agent: str,
        since_id: Optional[str] = None,
        mark_read: bool = True
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """
        Get messages for an agent.

        Args:
            for_agent: Agent ID to get messages for
            since_id: Only return messages after this ID
            mark_read: Whether to mark returned messages as read

        Returns:
            Tuple of (messages, cursor) where cursor is the last message ID.
        """
        with self._lock:
            # If no since_id provided, check persisted cursor for this agent
            if since_id is None:
                since_id = self._cursors.get(for_agent)

            # Find messages addressed to this agent or broadcast
            messages = []
            found_since = since_id is None  # If no since_id, include all

            for msg in self._state["messages"]:
                if not found_since:
                    if msg["id"] == since_id:
                        found_since = True
                    continue

                if msg["to"] == for_agent or msg["to"] == "broadcast":
                    messages.append(dict(msg))
                    if mark_read and not msg["read"]:
                        msg["read"] = True

            # Determine cursor (last message ID)
            cursor = messages[-1]["id"] if messages else since_id

            # Persist cursor for this agent
            if cursor:
                self._cursors[for_agent] = cursor
                self._flush_cursors()

            if mark_read and messages:
                self._flush()

            return messages, cursor

    def get_all_messages(self) -> list[dict[str, Any]]:
        """Get all messages (for debugging/dashboard)."""
        with self._lock:
            return [dict(m) for m in self._state["messages"]]

    def has_unread_messages_for(self, agent_id: str) -> bool:
        """
        Check if an agent has unread messages.

        Args:
            agent_id: Agent ID to check for unread messages.

        Returns:
            True if there are unread messages addressed to this agent.
        """
        with self._lock:
            for msg in self._state["messages"]:
                if (msg["to"] == agent_id or msg["to"] == "broadcast") and not msg["read"]:
                    return True
            return False

    # --- User Decision Operations ---

    def add_pending_decision(
        self,
        question: str,
        options: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """
        Add a pending user decision (from escalate_to_user).

        Args:
            question: Question to show to user
            options: Optional list of choices

        Returns:
            The created decision record.
        """
        with self._lock:
            decision = {
                "id": generate_id(),
                "question": question,
                "options": options or [],
                "asked_at": utc_now(),
                "answered_at": None,
                "answer": None
            }
            self._state["pending_user_decisions"].append(decision)
            self._flush()
            return dict(decision)

    def get_pending_decisions(self) -> list[dict[str, Any]]:
        """Get all unanswered decisions."""
        with self._lock:
            return [
                dict(d) for d in self._state["pending_user_decisions"]
                if d["answer"] is None
            ]

    def answer_decision(self, decision_id: str, answer: str) -> bool:
        """
        Record a user's answer to a decision.

        Returns:
            True if decision was found and updated, False otherwise.
        """
        with self._lock:
            for decision in self._state["pending_user_decisions"]:
                if decision["id"] == decision_id:
                    decision["answer"] = answer
                    decision["answered_at"] = utc_now()
                    self._flush()
                    return True
            return False

    # --- Task Operations ---

    def add_task(
        self,
        assigned_to: str,
        description: str
    ) -> dict[str, Any]:
        """
        Add a task assignment.

        Args:
            assigned_to: Agent ID the task is assigned to
            description: Task description

        Returns:
            The created task record.
        """
        with self._lock:
            task = {
                "id": generate_id(),
                "assigned_to": assigned_to,
                "description": description,
                "status": "pending",
                "created_at": utc_now(),
                "completed_at": None
            }
            self._state["tasks"].append(task)
            self._flush()
            return dict(task)

    def get_tasks(
        self,
        assigned_to: Optional[str] = None,
        status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """
        Get tasks with optional filters.

        Args:
            assigned_to: Filter by assigned agent
            status: Filter by status (pending, in_progress, done)
        """
        with self._lock:
            tasks = self._state["tasks"]

            if assigned_to is not None:
                tasks = [t for t in tasks if t["assigned_to"] == assigned_to]

            if status is not None:
                tasks = [t for t in tasks if t["status"] == status]

            return [dict(t) for t in tasks]

    def update_task(self, task_id: str, **updates: Any) -> Optional[dict[str, Any]]:
        """
        Update a task's fields.

        Args:
            task_id: Task to update
            **updates: Fields to update (status, etc.)

        Returns:
            Updated task record, or None if not found.

        Raises:
            InvalidStatusError: If status value is invalid.
        """
        # Validate status before acquiring lock
        if "status" in updates:
            validate_task_status(updates["status"])

        with self._lock:
            for task in self._state["tasks"]:
                if task["id"] == task_id:
                    task.update(updates)

                    # Auto-set completed_at when marked done
                    if updates.get("status") == "done" and task["completed_at"] is None:
                        task["completed_at"] = utc_now()

                    self._flush()
                    return dict(task)
            return None

    # --- Persistence ---

    def _get_state_file(self, name: str) -> Path:
        """Get path to a state file."""
        return self.state_dir / f"{name}.json"

    def _flush(self) -> None:
        """Flush all state to JSON files."""
        # Write separate files for each top-level key
        self._write_json("project", self._state["project"])
        self._write_json("agents", self._state["agents"])
        self._write_json("messages", self._state["messages"])
        self._write_json("pending_decisions", self._state["pending_user_decisions"])
        self._write_json("tasks", self._state["tasks"])

    def _flush_cursors(self) -> None:
        """Flush message cursors to JSON."""
        self._write_json("cursors", self._cursors)

    def _write_json(self, name: str, data: Any) -> None:
        """Write data to a JSON file atomically."""
        file_path = self._get_state_file(name)
        temp_path = file_path.with_suffix(".tmp")

        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)

        temp_path.replace(file_path)

    def reload(self) -> None:
        """Reload all state from JSON files. Used by standalone dashboard."""
        self._load()

    def _load(self) -> None:
        """Load existing state from JSON files."""
        # Load each state file if it exists
        project = self._load_json("project")
        if project:
            self._state["project"] = project

        agents = self._load_json("agents")
        if agents:
            self._state["agents"] = agents

        messages = self._load_json("messages")
        if messages:
            self._state["messages"] = messages

        decisions = self._load_json("pending_decisions")
        if decisions:
            self._state["pending_user_decisions"] = decisions

        tasks = self._load_json("tasks")
        if tasks:
            self._state["tasks"] = tasks

        cursors = self._load_json("cursors")
        if cursors:
            self._cursors = cursors

    def _load_json(self, name: str) -> Optional[Any]:
        """Load data from a JSON file. Returns None if file doesn't exist."""
        file_path = self._get_state_file(name)

        if not file_path.exists():
            return None

        try:
            with open(file_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    # --- Utility Methods ---

    def get_full_state(self) -> dict[str, Any]:
        """Get a copy of the full state (for debugging/testing)."""
        with self._lock:
            return {
                "project": dict(self._state["project"]),
                "agents": {k: dict(v) for k, v in self._state["agents"].items()},
                "messages": [dict(m) for m in self._state["messages"]],
                "pending_user_decisions": [dict(d) for d in self._state["pending_user_decisions"]],
                "tasks": [dict(t) for t in self._state["tasks"]]
            }

    def clear(self) -> None:
        """Clear all state (for testing)."""
        with self._lock:
            self._state = {
                "project": {
                    "name": "",
                    "description": "",
                    "repo": "",
                    "started_at": ""
                },
                "agents": {},
                "messages": [],
                "pending_user_decisions": [],
                "tasks": []
            }
            self._cursors = {}
            self._flush()
            self._flush_cursors()
