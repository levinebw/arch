#!/usr/bin/env python3
"""
Automated UAT (User Acceptance Tests) for ARCH.

Runs the real orchestrator with a mock claude binary that simulates
agent behavior via MCP tool calls. Validates the full lifecycle:
  1. Orchestrator starts and launches Archie
  2. Archie spawns worker agents
  3. Workers do work, commit files, report completion
  4. Archie resumes, merges branches, tears down agents
  5. Archie closes the project
  6. Orchestrator exits cleanly

No human intervention needed. The mock claude binary
(tests/mock_claude.py) replaces the real `claude` CLI.

Usage:
    # Run all UATs
    python -m pytest tests/test_uat.py -v -s --timeout=120

    # Run specific test
    python -m pytest tests/test_uat.py::TestUATMultiAgent -v -s --timeout=120
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest
import yaml

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from arch.orchestrator import Orchestrator
from arch.state import StateStore

logger = logging.getLogger(__name__)

# Path to the mock claude binary
MOCK_CLAUDE = str(Path(__file__).parent / "mock_claude.py")

# Maximum time to wait for the full UAT lifecycle
UAT_TIMEOUT = 90  # seconds


@pytest.fixture
def uat_project(tmp_path):
    """Create a fully-configured UAT project directory."""
    project_dir = tmp_path / "uat-project"
    project_dir.mkdir()

    # Initialize git repo
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    subprocess.run(
        ["git", "init"], cwd=project_dir,
        capture_output=True, env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "uat@test.com"],
        cwd=project_dir, capture_output=True, env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "UAT"],
        cwd=project_dir, capture_output=True, env=env, check=True,
    )
    # Create personas
    personas_dir = project_dir / "personas"
    personas_dir.mkdir()

    (personas_dir / "archie.md").write_text(
        "# Archie - Lead Agent\n\nYou are Archie, the coordinator.\n"
        "## Session Startup\n1. Call get_project_context\n"
        "2. Spawn agents\n## Completing Work\nMerge and teardown.\n"
    )
    (personas_dir / "frontend.md").write_text(
        "# Frontend Developer\nYou build UI with HTML/CSS/JS.\n"
    )
    (personas_dir / "qa.md").write_text(
        "# QA Engineer\nYou write tests.\n"
    )

    # Create BRIEF.md
    (project_dir / "BRIEF.md").write_text(
        "# Portfolio Site\n\n## Goal\nBuild a multi-page portfolio.\n\n"
        "## Done When\n- index.html exists\n- test_site.py passes\n\n"
        "## Current Status\nNot started.\n\n"
        "## Decisions Log\n| Date | Decision |\n|------|----------|\n"
    )

    # Create arch.yaml
    config = {
        "project": {
            "name": "UAT Portfolio",
            "description": "Automated UAT test project",
            "repo": str(project_dir),
        },
        "archie": {
            "persona": "personas/archie.md",
            "model": "claude-sonnet-4-5",
        },
        "agent_pool": [
            {
                "id": "frontend",
                "persona": "personas/frontend.md",
                "model": "claude-sonnet-4-5",
                "max_instances": 1,
            },
            {
                "id": "qa",
                "persona": "personas/qa.md",
                "model": "claude-sonnet-4-5",
                "max_instances": 1,
            },
        ],
        "settings": {
            "max_concurrent_agents": 5,
            "state_dir": str(project_dir / "state"),
            "mcp_port": 0,  # overwritten by test
            "token_budget_usd": 10.0,
        },
    }
    (project_dir / "arch.yaml").write_text(yaml.dump(config))

    # Create .gitignore
    (project_dir / ".gitignore").write_text("state/\n.worktrees/\n__pycache__/\n")

    # Initial commit
    subprocess.run(
        ["git", "add", "."], cwd=project_dir,
        capture_output=True, env=env, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial UAT project setup"],
        cwd=project_dir, capture_output=True, env=env, check=True,
    )
    # Rename default branch to 'main' (GIT_CONFIG_GLOBAL=/dev/null defaults to 'master')
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=project_dir, capture_output=True, env=env, check=True,
    )

    return project_dir


def make_mock_claude_wrapper(mock_path: str) -> Path:
    """Create a shell wrapper that calls mock_claude.py via python.

    This replaces the `claude` binary in PATH so session.py finds it.
    Returns the directory containing the wrapper (prepend to PATH).
    """
    wrapper_dir = Path(mock_path).parent / "mock_bin"
    wrapper_dir.mkdir(exist_ok=True)

    wrapper = wrapper_dir / "claude"
    wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} {MOCK_CLAUDE} \"$@\"\n")
    wrapper.chmod(0o755)

    return wrapper_dir


class UATResult:
    """Collects and validates UAT outcomes."""

    def __init__(self, project_dir: Path, state_dir: Path):
        self.project_dir = project_dir
        self.state_dir = state_dir
        self.errors: list[str] = []
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.checks.append((name, condition, detail))
        if not condition:
            self.errors.append(f"FAIL: {name} — {detail}")

    def load_state(self) -> StateStore:
        return StateStore(self.state_dir)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        lines = ["\n=== UAT Results ==="]
        for name, ok, detail in self.checks:
            status = "PASS" if ok else "FAIL"
            line = f"  [{status}] {name}"
            if detail:
                line += f" — {detail}"
            lines.append(line)
        lines.append(f"\n{'ALL CHECKS PASSED' if self.passed else f'{len(self.errors)} FAILED'}")
        return "\n".join(lines)


def validate_multi_agent_uat(project_dir: Path, state_dir: Path) -> UATResult:
    """Validate outcomes of the multi-agent UAT."""
    result = UATResult(project_dir, state_dir)
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    # 1. State files exist
    result.check(
        "State directory exists",
        state_dir.exists(),
    )
    result.check(
        "agents.json exists",
        (state_dir / "agents.json").exists(),
    )
    result.check(
        "messages.json exists",
        (state_dir / "messages.json").exists(),
    )

    # 2. Check messages — read raw file since get_messages filters by cursor
    messages_path = state_dir / "messages.json"
    all_messages = []
    if messages_path.exists():
        try:
            all_messages = json.loads(messages_path.read_text())
        except json.JSONDecodeError:
            pass

    message_dump = json.dumps(all_messages, indent=2)[:500] if all_messages else "[]"
    result.check(
        "Messages were exchanged",
        len(all_messages) > 0,
        f"got {len(all_messages)} messages",
    )

    # Check that agents were spawned — look for evidence in messages,
    # current agent state, and project completion messages.
    # Note: teardown_agent removes agents from state.
    message_text_dump = " ".join(
        m.get("content", "") + " " + m.get("from", "") + " " + m.get("to", "")
        for m in all_messages
    )

    result.check(
        "Frontend agent evidence",
        "frontend" in message_text_dump,
        f"message dump: {message_dump}",
    )
    result.check(
        "QA agent evidence",
        "qa" in message_text_dump,
        f"message dump: {message_dump}",
    )

    # 4. Project status
    project_data = None
    project_path = state_dir / "project.json"
    if project_path.exists():
        try:
            project_data = json.loads(project_path.read_text())
        except json.JSONDecodeError:
            pass

    result.check(
        "Project marked complete",
        project_data is not None and project_data.get("status") == "complete",
        f"project status: {project_data.get('status') if project_data else 'no project.json'}",
    )

    # 5. Git state diagnostics
    branch_result = subprocess.run(
        ["git", "branch", "-a"],
        cwd=project_dir, capture_output=True, text=True, env=env,
    )
    result.check(
        "Git branches accessible",
        branch_result.returncode == 0,
        f"branches:\n{branch_result.stdout.strip()}",
    )

    # 6. Check for delivered files on main branch
    git_log = subprocess.run(
        ["git", "log", "--oneline", "--all", "-15"],
        cwd=project_dir, capture_output=True, text=True, env=env,
    )
    all_commits = git_log.stdout.strip()
    result.check(
        "Git has commits beyond initial",
        all_commits.count("\n") >= 1 or "deliver" in all_commits or "merge" in all_commits.lower(),
        f"git log --all:\n{all_commits}",
    )

    # Check if expected files exist on main (after merge)
    files_on_main = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, env=env,
    )
    result.check(
        "Files delivered to main",
        "index.html" in files_on_main.stdout or "test_site.py" in files_on_main.stdout,
        f"files on HEAD:\n{files_on_main.stdout.strip()}",
    )

    # 7. Token usage was tracked
    usage_path = state_dir / "usage.json"
    if usage_path.exists():
        try:
            usage = json.loads(usage_path.read_text())
            has_usage = bool(usage)
        except json.JSONDecodeError:
            has_usage = False
    else:
        has_usage = False

    result.check(
        "Token usage tracked",
        has_usage,
        f"usage.json exists: {usage_path.exists()}",
    )

    return result


class TestUATMultiAgent:
    """Full multi-agent UAT with mock claude CLI."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(UAT_TIMEOUT)
    async def test_full_lifecycle(self, uat_project, tmp_path):
        """Run the complete multi-agent lifecycle and validate outcomes."""

        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
        logging.getLogger("uvicorn").setLevel(logging.WARNING)

        project_dir = uat_project
        config_path = project_dir / "arch.yaml"

        # Create mock claude wrapper and put it first in PATH
        mock_bin_dir = make_mock_claude_wrapper(str(tmp_path))
        env_path = f"{mock_bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["PATH"] = env_path

        # Verify mock is found
        which_result = subprocess.run(
            ["which", "claude"], capture_output=True, text=True,
        )
        assert "mock_bin" in which_result.stdout, \
            f"Mock claude not in PATH: {which_result.stdout}"

        logger.info(f"UAT project: {project_dir}")
        logger.info(f"Mock claude: {mock_bin_dir / 'claude'}")

        # Find a free port for the MCP server
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        # Update config with the free port
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["settings"]["mcp_port"] = free_port
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        logger.info(f"MCP port: {free_port}")

        # Create and start orchestrator
        orchestrator = Orchestrator(config_path, keep_worktrees=False)

        started = await orchestrator.startup()
        assert started, "Orchestrator failed to start"

        logger.info("Orchestrator started, running main loop...")

        # Run the orchestrator's main loop (which includes auto-resume logic)
        # in a background task, and monitor for completion.
        run_task = asyncio.create_task(orchestrator.run())
        try:
            await asyncio.wait_for(
                self._wait_for_project_complete(orchestrator),
                timeout=UAT_TIMEOUT - 10,
            )
        except asyncio.TimeoutError:
            logger.warning("UAT timed out — checking partial results")
        finally:
            orchestrator._shutdown_requested = True
            # Wait briefly for run() to exit its loop
            try:
                await asyncio.wait_for(run_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                run_task.cancel()
            await orchestrator.shutdown()

        # Validate outcomes
        state_dir = Path(config["settings"]["state_dir"])
        if not state_dir.is_absolute():
            state_dir = (project_dir / state_dir).resolve()

        result = validate_multi_agent_uat(project_dir, state_dir)
        logger.info(result.summary())

        # Print full state for debugging (before assert so it always shows)
        self._dump_state(state_dir)

        if not result.passed:
            self._dump_state(state_dir)

        assert result.passed, f"UAT failed:\n{result.summary()}"

    async def _wait_for_project_complete(self, orchestrator: Orchestrator) -> None:
        """Poll until the orchestrator marks the project complete."""
        while True:
            if orchestrator._project_complete:
                logger.info("Project marked complete")
                await asyncio.sleep(2)  # Let cleanup finish
                return
            await asyncio.sleep(1)

    def _dump_state(self, state_dir: Path) -> None:
        """Dump all state files and git state for debugging."""
        logger.info("=== State dump ===")
        for json_file in sorted(state_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text())
                logger.info(f"{json_file.name}: {json.dumps(data, indent=2)[:500]}")
            except Exception as e:
                logger.info(f"{json_file.name}: error reading: {e}")

        # Dump git state from project dir
        project_dir = state_dir.parent
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        for cmd in [
            ["git", "branch", "-a"],
            ["git", "log", "--oneline", "--all", "-15"],
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            ["git", "worktree", "list"],
        ]:
            r = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, env=env)
            logger.info(f"$ {' '.join(cmd)}\n{r.stdout.strip()}\n{r.stderr.strip()}")


class TestUATSingleAgent:
    """Single-agent UAT — just Archie spawning one worker."""

    @pytest.fixture
    def single_agent_project(self, tmp_path):
        """Create a single-agent UAT project."""
        project_dir = tmp_path / "uat-single"
        project_dir.mkdir()

        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"

        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, env=env, check=True)
        subprocess.run(["git", "config", "user.email", "uat@test.com"], cwd=project_dir, capture_output=True, env=env, check=True)
        subprocess.run(["git", "config", "user.name", "UAT"], cwd=project_dir, capture_output=True, env=env, check=True)

        personas_dir = project_dir / "personas"
        personas_dir.mkdir()
        (personas_dir / "archie.md").write_text("# Archie\nYou are the lead agent coordinator.\n")
        (personas_dir / "frontend.md").write_text("# Frontend Dev\nBuild HTML.\n")

        (project_dir / "BRIEF.md").write_text(
            "# Test Project\n## Goal\nBuild a page.\n## Done When\n- index.html\n"
            "## Current Status\nNot started.\n## Decisions Log\n| Date | Decision |\n|------|----------|\n"
        )

        config = {
            "project": {"name": "Single Agent UAT", "description": "test", "repo": str(project_dir)},
            "archie": {"persona": "personas/archie.md", "model": "claude-sonnet-4-5"},
            "agent_pool": [
                {"id": "frontend", "persona": "personas/frontend.md", "model": "claude-sonnet-4-5", "max_instances": 1},
            ],
            "settings": {"max_concurrent_agents": 3, "state_dir": str(project_dir / "state"), "mcp_port": 0, "token_budget_usd": 5.0},
        }
        (project_dir / "arch.yaml").write_text(yaml.dump(config))
        (project_dir / ".gitignore").write_text("state/\n.worktrees/\n")

        subprocess.run(["git", "add", "."], cwd=project_dir, capture_output=True, env=env, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, capture_output=True, env=env, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=project_dir, capture_output=True, env=env, check=True)

        return project_dir

    @pytest.mark.asyncio
    @pytest.mark.timeout(UAT_TIMEOUT)
    async def test_single_agent_lifecycle(self, single_agent_project, tmp_path):
        """Archie spawns one frontend agent, it delivers, Archie merges and closes."""
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("uvicorn").setLevel(logging.WARNING)

        project_dir = single_agent_project
        config_path = project_dir / "arch.yaml"

        mock_bin_dir = make_mock_claude_wrapper(str(tmp_path))
        os.environ["PATH"] = f"{mock_bin_dir}:{os.environ.get('PATH', '')}"

        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["settings"]["mcp_port"] = free_port
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        orchestrator = Orchestrator(config_path, keep_worktrees=False)
        started = await orchestrator.startup()
        assert started, "Orchestrator failed to start"

        run_task = asyncio.create_task(orchestrator.run())
        try:
            await asyncio.wait_for(
                self._wait_for_completion(orchestrator),
                timeout=UAT_TIMEOUT - 10,
            )
        except asyncio.TimeoutError:
            logger.warning("Single agent UAT timed out")
        finally:
            orchestrator._shutdown_requested = True
            try:
                await asyncio.wait_for(run_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                run_task.cancel()
            await orchestrator.shutdown()

        # Basic validation
        state_dir = Path(config["settings"]["state_dir"])
        state = StateStore(state_dir)
        agents = state.list_agents()
        agent_ids = [a["id"] for a in agents]

        assert "archie" in agent_ids, f"Archie not found in agents: {agent_ids}"
        assert any(a["role"] == "frontend" for a in agents), f"No frontend agent: {agents}"

        logger.info(f"Agents: {json.dumps(agents, indent=2)}")
        logger.info("Single agent UAT complete")

    async def _wait_for_completion(self, orchestrator: Orchestrator) -> None:
        while True:
            if orchestrator._project_complete:
                await asyncio.sleep(1)
                return
            await asyncio.sleep(1)


class TestUATDashboard:
    """Validate that dashboard can read state written by orchestrator."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_dashboard_reads_orchestrator_state(self, uat_project):
        """Write state as orchestrator would, verify dashboard's StateStore reads it."""
        project_dir = uat_project
        state_dir = (project_dir / "state").resolve()
        state_dir.mkdir(exist_ok=True)

        # Write state as orchestrator
        state = StateStore(state_dir)
        state.init_project("UAT", "test project", str(project_dir))
        state.register_agent("archie", "archie", str(project_dir), sandboxed=False)
        state.register_agent("frontend-1", "frontend", str(project_dir / ".worktrees" / "frontend-1"), sandboxed=False)
        state.add_message("archie", "frontend-1", "Build the site")
        state.update_agent("frontend-1", status="working")

        # Read state as dashboard would (new StateStore instance, same dir)
        dashboard_state = StateStore(state_dir)
        agents = dashboard_state.list_agents()
        messages = dashboard_state.get_messages("frontend-1")

        assert len(agents) == 2
        assert agents[0]["id"] == "archie" or agents[1]["id"] == "archie"
        assert any(a["id"] == "frontend-1" and a["status"] == "working" for a in agents)
        assert len(messages) >= 1

        # Verify reload picks up changes
        state.update_agent("frontend-1", status="done")
        dashboard_state.reload()
        updated_agents = dashboard_state.list_agents()
        frontend = [a for a in updated_agents if a["id"] == "frontend-1"][0]
        assert frontend["status"] == "done"

        logger.info("Dashboard state read test passed")
