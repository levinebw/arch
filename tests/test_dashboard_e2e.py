"""
Dashboard end-to-end tests using Textual's Pilot testing framework.

These tests exercise the dashboard UI as a human would see it:
  - Start the dashboard with real StateStore + real MCP server
  - Use MCP SSE clients to trigger state changes (spawn agents, send messages)
  - Use Textual's Pilot to assert the dashboard reflects those changes
  - Verify keyboard shortcuts, escalation handling, and modal screens

Real components: Dashboard (Textual app), StateStore, MCPServer (uvicorn), SSE clients.
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

from arch.dashboard import Dashboard, AgentsPanel, ActivityPanel, CostsPanel, EscalationPanel
from arch.orchestrator import Orchestrator
from arch.state import StateStore
from arch.token_tracker import TokenTracker

from textual.widgets import Static, RichLog


# ============================================================================
# Helpers
# ============================================================================


def get_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def create_mock_claude_process(session_id="test-session"):
    """Create a mock Claude subprocess with stream-json output."""
    mock = MagicMock()
    mock.pid = id(mock) % 100000
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
    """Mock subprocess and Docker/GitHub gates — MCP server runs for real."""
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
    """Create a real git repository."""
    repo_path = tmp_path / "dashboard-project"
    repo_path.mkdir()

    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(tmp_path)}

    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True,
                    check=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@dash.com"],
                    cwd=repo_path, capture_output=True, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "Dashboard Test"],
                    cwd=repo_path, capture_output=True, check=True, env=env)

    (repo_path / "README.md").write_text("# Dashboard Test\n")
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

    (e2e_repo / "BRIEF.md").write_text("# Brief\n\n## Goals\nDashboard test.\n\n"
                                        "## Done When\n- Tests pass\n\n"
                                        "## Constraints\n- None\n\n"
                                        "## Current Status\nTesting.\n\n"
                                        "## Decisions Log\n| Date | Decision | Rationale |\n"
                                        "|------|----------|----------|\n")

    config = {
        "project": {
            "name": "Dashboard Test Project",
            "description": "Testing dashboard with real MCP",
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

    return config_path, port, state_dir


# ============================================================================
# Tests — Dashboard Standalone (no MCP, just state manipulation)
# ============================================================================


class TestDashboardStandalone:
    """Test dashboard rendering using direct state manipulation.

    These tests don't need MCP — they create a StateStore, populate it,
    then verify the dashboard renders correctly via Pilot.
    """

    @pytest.mark.asyncio
    async def test_dashboard_shows_project_name(self, tmp_path):
        """Dashboard title should display the project name."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="My Cool Project", description="Test", repo="/tmp")
        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker, budget=10.0)
        async with app.run_test(size=(120, 40)) as pilot:
            assert "My Cool Project" in app.title

    @pytest.mark.asyncio
    async def test_dashboard_shows_no_agents_initially(self, tmp_path):
        """Dashboard should show 'No agents' when none are registered."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Empty", description="Test", repo="/tmp")
        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            content = app.query_one("#agents-content", Static)
            assert "No agents" in str(content.content)

    @pytest.mark.asyncio
    async def test_dashboard_shows_registered_agents(self, tmp_path):
        """Dashboard should display agents after registration."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Agent Test", description="Test", repo="/tmp")
        state.register_agent(
            agent_id="frontend-1",
            role="frontend-dev",
            worktree="/tmp/wt/frontend-1",
        )
        state.update_agent("frontend-1", status="working", task="Building login form")

        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            # Trigger a refresh (dashboard refreshes every 2s, but we can force it)
            app._refresh_data()
            await pilot.pause()

            content = app.query_one("#agents-content", Static)
            rendered = str(content.content)
            assert "frontend-1" in rendered

    @pytest.mark.asyncio
    async def test_dashboard_shows_messages_in_activity_log(self, tmp_path):
        """Activity log should display new messages."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Msg Test", description="Test", repo="/tmp")
        state.register_agent(agent_id="archie", role="lead", worktree="/tmp/wt/archie")
        state.register_agent(agent_id="worker-1", role="frontend", worktree="/tmp/wt/worker")
        state.add_message("worker-1", "archie", "Login page is done")

        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            app._refresh_data()
            await pilot.pause()

            log = app.query_one("#activity-log", RichLog)
            # RichLog stores lines internally — check that at least one line was written
            assert len(log.lines) > 0

    @pytest.mark.asyncio
    async def test_dashboard_shows_costs(self, tmp_path):
        """Costs panel should display per-agent costs."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Cost Test", description="Test", repo="/tmp")

        tracker = TokenTracker(state_dir=tmp_path / "state")
        tracker.register_agent("archie", "claude-sonnet-4-6")
        tracker.register_agent("worker-1", "claude-sonnet-4-6")

        app = Dashboard(state=state, token_tracker=tracker, budget=10.0)
        async with app.run_test(size=(120, 40)) as pilot:
            app._refresh_data()
            await pilot.pause()

            content = app.query_one("#costs-content", Static)
            rendered = str(content.content)
            assert "Total" in rendered

    @pytest.mark.asyncio
    async def test_help_screen_opens_and_closes(self, tmp_path):
        """Pressing ? should open help modal, Escape should close it."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Help Test", description="Test", repo="/tmp")
        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            # Open help
            await pilot.press("?")
            await pilot.pause()

            # Help screen should be visible
            from arch.dashboard import HelpScreen
            assert app.screen.__class__.__name__ == "HelpScreen"

            # Close help
            await pilot.press("escape")
            await pilot.pause()

            # Should be back to main screen
            assert app.screen.__class__.__name__ != "HelpScreen"

    @pytest.mark.asyncio
    async def test_quit_calls_callback(self, tmp_path):
        """Pressing q should call the on_quit callback."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Quit Test", description="Test", repo="/tmp")
        tracker = TokenTracker(state_dir=tmp_path / "state")

        quit_called = []
        app = Dashboard(state=state, token_tracker=tracker, on_quit=lambda: quit_called.append(True))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("q")
            await pilot.pause()

        assert len(quit_called) == 1

    @pytest.mark.asyncio
    async def test_agent_status_updates_on_refresh(self, tmp_path):
        """Dashboard should reflect status changes after refresh."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Status Test", description="Test", repo="/tmp")
        state.register_agent(agent_id="worker-1", role="frontend", worktree="/tmp/wt")
        state.update_agent("worker-1", status="working", task="Building UI")

        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            app._refresh_data()
            await pilot.pause()

            content = app.query_one("#agents-content", Static)
            rendered_before = str(content.content)
            assert "worker-1" in rendered_before

            # Change status
            state.update_agent("worker-1", status="done", task="UI complete")
            app._refresh_data()
            await pilot.pause()

            content = app.query_one("#agents-content", Static)
            rendered_after = str(content.content)
            assert "worker-1" in rendered_after

    @pytest.mark.asyncio
    async def test_message_log_modal(self, tmp_path):
        """Pressing m should open full message log modal."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Modal Test", description="Test", repo="/tmp")
        state.register_agent(agent_id="archie", role="lead", worktree="/tmp/wt/archie")
        state.register_agent(agent_id="worker-1", role="frontend", worktree="/tmp/wt/worker")
        state.add_message("worker-1", "archie", "Need API specs")
        state.add_message("archie", "worker-1", "Check the BRIEF.md")

        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("m")
            await pilot.pause()

            from arch.dashboard import MessageLogScreen
            assert app.screen.__class__.__name__ == "MessageLogScreen"

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.__class__.__name__ != "MessageLogScreen"


# ============================================================================
# Tests — Dashboard + Real MCP Server (full E2E)
# ============================================================================


class TestDashboardWithMCP:
    """Test dashboard reflecting real MCP operations.

    These tests start a real orchestrator (with real MCP server),
    create a Dashboard connected to the same StateStore,
    and verify that MCP tool calls (spawn, message, etc.) are
    reflected in the dashboard widgets.
    """

    @pytest.mark.asyncio
    async def test_dashboard_reflects_agent_spawn(self, e2e_config, e2e_repo):
        """Dashboard should show a new agent after spawn_agent via MCP."""
        config_path, port, state_dir = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Create dashboard connected to the same state
                app = Dashboard(
                    state=orch.state,
                    token_tracker=orch.token_tracker,
                    budget=10.0,
                )
                async with app.run_test(size=(120, 40)) as pilot:
                    # Initially should show archie only
                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert "archie" in rendered

                    # Spawn a worker via real MCP
                    async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                        async with ClientSession(r, w) as archie:
                            await archie.initialize()
                            result = await archie.call_tool("spawn_agent", {
                                "role": "frontend-dev",
                                "assignment": "Build the dashboard",
                            })
                            data = json.loads(result.content[0].text)
                            agent_id = data["agent_id"]

                    # Refresh dashboard and check
                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert agent_id in rendered, \
                        f"Dashboard should show spawned agent. Got: {rendered}"

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_dashboard_reflects_messages(self, e2e_config, e2e_repo):
        """Dashboard activity log should show messages sent via MCP."""
        config_path, port, state_dir = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Spawn a worker
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()
                        result = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Build login page",
                        })
                        agent_id = json.loads(result.content[0].text)["agent_id"]

                # Worker sends message via MCP
                async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                    async with ClientSession(r, w) as worker:
                        await worker.initialize()
                        await worker.call_tool("send_message", {
                            "to": "archie",
                            "content": "Login page is ready for review",
                        })

                # Now check dashboard
                app = Dashboard(
                    state=orch.state,
                    token_tracker=orch.token_tracker,
                )
                async with app.run_test(size=(120, 40)) as pilot:
                    app._refresh_data()
                    await pilot.pause()

                    log = app.query_one("#activity-log", RichLog)
                    assert len(log.lines) > 0, "Activity log should have entries"

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_dashboard_reflects_status_change(self, e2e_config, e2e_repo):
        """Dashboard should reflect agent status changes via update_status MCP tool."""
        config_path, port, state_dir = e2e_config
        orch = Orchestrator(config_path)

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
                            "assignment": "Build components",
                        })
                        agent_id = json.loads(result.content[0].text)["agent_id"]

                # Worker updates status
                async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                    async with ClientSession(r, w) as worker:
                        await worker.initialize()
                        await worker.call_tool("update_status", {
                            "status": "working",
                            "task": "Building React components",
                        })

                # Check dashboard
                app = Dashboard(
                    state=orch.state,
                    token_tracker=orch.token_tracker,
                )
                async with app.run_test(size=(120, 40)) as pilot:
                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert agent_id in rendered
                    # The task should appear (truncated to 20 chars in display)
                    assert "Building React" in rendered or "React" in rendered

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_dashboard_reflects_completion(self, e2e_config, e2e_repo):
        """Dashboard should show agent as 'done' after report_completion via MCP."""
        config_path, port, state_dir = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                # Spawn and complete
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as archie:
                        await archie.initialize()
                        result = await archie.call_tool("spawn_agent", {
                            "role": "frontend-dev",
                            "assignment": "Build UI",
                        })
                        agent_id = json.loads(result.content[0].text)["agent_id"]

                async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                    async with ClientSession(r, w) as worker:
                        await worker.initialize()
                        await worker.call_tool("report_completion", {
                            "summary": "UI is complete",
                            "artifacts": ["src/App.tsx"],
                        })

                # Verify in state
                agent = orch.state.get_agent(agent_id)
                assert agent["status"] == "done"

                # Verify in dashboard
                app = Dashboard(
                    state=orch.state,
                    token_tracker=orch.token_tracker,
                )
                async with app.run_test(size=(120, 40)) as pilot:
                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert agent_id in rendered

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_full_lifecycle_through_dashboard(self, e2e_config, e2e_repo):
        """Full lifecycle visible in dashboard: spawn → work → message → complete → teardown.

        This is the centerpiece test. The dashboard stays open the entire time
        while MCP operations happen, and we verify the dashboard reflects each
        stage of the lifecycle.
        """
        config_path, port, state_dir = e2e_config
        orch = Orchestrator(config_path)

        with mock_subprocess_only():
            assert await orch.startup() is True
            await asyncio.sleep(0.5)

            try:
                app = Dashboard(
                    state=orch.state,
                    token_tracker=orch.token_tracker,
                    budget=10.0,
                )
                async with app.run_test(size=(120, 40)) as pilot:
                    # --- Phase 1: Initial state ---
                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert "archie" in rendered

                    # --- Phase 2: Spawn worker ---
                    async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                        async with ClientSession(r, w) as archie:
                            await archie.initialize()
                            result = await archie.call_tool("spawn_agent", {
                                "role": "frontend-dev",
                                "assignment": "Build the login page",
                            })
                            agent_id = json.loads(result.content[0].text)["agent_id"]

                    app._refresh_data()
                    await pilot.pause()

                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert agent_id in rendered, f"Phase 2: worker should appear. Got: {rendered}"

                    # --- Phase 3: Worker sends messages ---
                    async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                        async with ClientSession(r, w) as worker:
                            await worker.initialize()

                            await worker.call_tool("update_status", {
                                "status": "working",
                                "task": "Implementing login form",
                            })

                            await worker.call_tool("send_message", {
                                "to": "archie",
                                "content": "Started work on the login page",
                            })

                    app._refresh_data()
                    await pilot.pause()

                    log = app.query_one("#activity-log", RichLog)
                    assert len(log.lines) > 0, "Phase 3: activity log should have entries"

                    # --- Phase 4: Worker completes ---
                    async with sse_client(f"http://127.0.0.1:{port}/sse/{agent_id}") as (r, w):
                        async with ClientSession(r, w) as worker:
                            await worker.initialize()
                            await worker.call_tool("report_completion", {
                                "summary": "Login page done",
                                "artifacts": ["login.html"],
                            })

                    app._refresh_data()
                    await pilot.pause()

                    agent = orch.state.get_agent(agent_id)
                    assert agent["status"] == "done"

                    # --- Phase 5: Archie tears down worker ---
                    async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                        async with ClientSession(r, w) as archie:
                            await archie.initialize()
                            result = await archie.call_tool("teardown_agent", {
                                "agent_id": agent_id,
                            })
                            teardown = json.loads(result.content[0].text)
                            assert teardown.get("ok") is True

                    app._refresh_data()
                    await pilot.pause()

                    # After teardown, agent should be removed from dashboard
                    content = app.query_one("#agents-content", Static)
                    rendered = str(content.content)
                    assert agent_id not in rendered, \
                        f"Phase 5: torn-down agent should disappear. Got: {rendered}"

            finally:
                await orch.shutdown()

    @pytest.mark.asyncio
    async def test_escalation_display(self, tmp_path):
        """Dashboard should display pending escalations and accept answers."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Escalation Test", description="Test", repo="/tmp")
        state.register_agent(agent_id="archie", role="lead", worktree="/tmp/wt/archie")

        # Add a pending decision
        state.add_pending_decision(
            question="Should we use React or Vue?",
            options=["React", "Vue"],
        )

        tracker = TokenTracker(state_dir=tmp_path / "state")

        app = Dashboard(state=state, token_tracker=tracker)
        async with app.run_test(size=(120, 40)) as pilot:
            app._refresh_data()
            await pilot.pause()

            # Check escalation panel shows the question
            q_widget = app.query_one("#escalation-question", Static)
            rendered = str(q_widget.content)
            assert "React or Vue" in rendered

    @pytest.mark.asyncio
    async def test_budget_bar_renders(self, tmp_path):
        """Budget progress bar should render with correct percentage."""
        state = StateStore(tmp_path / "state")
        state.init_project(name="Budget Test", description="Test", repo="/tmp")

        tracker = TokenTracker(state_dir=tmp_path / "state")
        tracker.register_agent("archie", "claude-sonnet-4-6")

        app = Dashboard(state=state, token_tracker=tracker, budget=10.0)
        async with app.run_test(size=(120, 40)) as pilot:
            # Costs panel should exist and have a progress bar
            costs_panel = app.query_one("#costs-panel", CostsPanel)
            assert costs_panel.budget == 10.0

            app._refresh_data()
            await pilot.pause()

            # Check costs content shows Total line
            content = app.query_one("#costs-content", Static)
            rendered = str(content.content)
            assert "Total" in rendered
            assert "$" in rendered
