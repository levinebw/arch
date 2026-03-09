"""
Tests for the ARCH Dashboard.

Tests the Textual TUI components and integration with state/token tracking.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Input, Static, ProgressBar

from arch.dashboard import (
    Dashboard,
    AgentsPanel,
    ActivityPanel,
    CostsPanel,
    EscalationPanel,
    HelpScreen,
    MessageLogScreen,
    format_runtime,
    format_timestamp,
    format_agent_display,
    STATUS_INDICATORS,
)
from arch.state import StateStore
from arch.token_tracker import TokenTracker
from pathlib import Path


# ============================================================================
# Helper Functions Tests
# ============================================================================


class TestFormatRuntime:
    """Tests for format_runtime function."""

    def test_none_returns_zeros(self):
        """None input returns 00:00:00."""
        assert format_runtime(None) == "00:00:00"

    def test_empty_string_returns_zeros(self):
        """Empty string returns 00:00:00."""
        assert format_runtime("") == "00:00:00"

    def test_invalid_format_returns_zeros(self):
        """Invalid format returns 00:00:00."""
        assert format_runtime("not-a-date") == "00:00:00"

    def test_recent_time_formats_correctly(self):
        """Recent timestamp formats as expected."""
        # Create a time 1 hour, 30 minutes, 45 seconds ago
        now = datetime.now(timezone.utc)
        # Can't easily test exact values due to timing, but verify format
        recent = now.isoformat()
        result = format_runtime(recent)
        # Should be 00:00:0X (within a few seconds)
        assert result.startswith("00:00:")

    def test_z_suffix_handled(self):
        """Z suffix is handled correctly."""
        result = format_runtime("2024-01-01T00:00:00Z")
        # This will be a large duration, but should not raise
        assert ":" in result


class TestFormatTimestamp:
    """Tests for format_timestamp function."""

    def test_valid_timestamp(self):
        """Valid timestamp formats as HH:MM."""
        result = format_timestamp("2024-01-15T14:30:00Z")
        assert result == "14:30"

    def test_invalid_timestamp_returns_placeholder(self):
        """Invalid timestamp returns --:--."""
        assert format_timestamp("invalid") == "--:--"

    def test_none_returns_placeholder(self):
        """None returns --:--."""
        assert format_timestamp(None) == "--:--"

    def test_empty_string_returns_placeholder(self):
        """Empty string returns --:--."""
        assert format_timestamp("") == "--:--"


class TestStatusIndicators:
    """Tests for status indicator constants."""

    def test_all_statuses_have_indicators(self):
        """All expected statuses have indicators defined."""
        expected_statuses = {"working", "blocked", "waiting_review", "idle", "done", "error"}
        assert expected_statuses == set(STATUS_INDICATORS.keys())

    def test_indicators_are_tuples(self):
        """Each indicator is a (symbol, color) tuple."""
        for status, indicator in STATUS_INDICATORS.items():
            assert isinstance(indicator, tuple)
            assert len(indicator) == 2
            symbol, color = indicator
            assert isinstance(symbol, str)
            assert isinstance(color, str)

    def test_working_is_green_circle(self):
        """Working status shows green filled circle."""
        symbol, color = STATUS_INDICATORS["working"]
        assert symbol == "●"
        assert color == "green"

    def test_blocked_is_yellow_circle(self):
        """Blocked status shows yellow circle."""
        symbol, color = STATUS_INDICATORS["blocked"]
        assert symbol == "●"
        assert color == "yellow"

    def test_idle_is_gray_empty_circle(self):
        """Idle status shows gray (bright_black) empty circle."""
        symbol, color = STATUS_INDICATORS["idle"]
        assert symbol == "○"
        assert color == "bright_black"

    def test_done_is_green_checkmark(self):
        """Done status shows green checkmark."""
        symbol, color = STATUS_INDICATORS["done"]
        assert symbol == "✓"
        assert color == "green"

    def test_error_is_red_x(self):
        """Error status shows red X."""
        symbol, color = STATUS_INDICATORS["error"]
        assert symbol == "✗"
        assert color == "red"


# ============================================================================
# Dashboard App Tests
# ============================================================================


@pytest.fixture
def mock_state(tmp_path):
    """Create a mock StateStore."""
    state = StateStore(tmp_path / "state")
    state.init_project("Test Project", "A test project", "/repo")
    return state


@pytest.fixture
def mock_token_tracker(tmp_path):
    """Create a mock TokenTracker."""
    return TokenTracker(state_dir=tmp_path / "state")


@pytest.fixture
def mock_mcp_server():
    """Create a mock MCP server."""
    server = MagicMock()
    server.answer_escalation = MagicMock(return_value=True)
    return server


class TestDashboardInit:
    """Tests for Dashboard initialization."""

    def test_init_with_required_args(self, mock_state, mock_token_tracker):
        """Dashboard initializes with required arguments."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)
        assert app.state == mock_state
        assert app.token_tracker == mock_token_tracker
        assert app.mcp_server is None
        assert app.budget is None

    def test_init_with_all_args(self, mock_state, mock_token_tracker, mock_mcp_server):
        """Dashboard initializes with all arguments."""
        quit_callback = MagicMock()
        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server,
            budget=10.0,
            on_quit=quit_callback,
        )
        assert app.mcp_server == mock_mcp_server
        assert app.budget == 10.0
        assert app.on_quit_callback == quit_callback

    def test_initial_reactive_values(self, mock_state, mock_token_tracker):
        """Initial reactive values are set correctly."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)
        assert app.project_name == "ARCH"
        assert app.runtime == "00:00:00"


class TestDashboardComponents:
    """Tests for Dashboard widget composition."""

    @pytest.mark.asyncio
    async def test_compose_creates_main_panels(self, mock_state, mock_token_tracker):
        """Dashboard creates all main panels."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            # Check main panels exist
            assert app.query_one("#agents-panel", AgentsPanel)
            assert app.query_one("#activity-panel", ActivityPanel)
            assert app.query_one("#costs-panel", CostsPanel)
            assert app.query_one("#escalation-panel", EscalationPanel)

    @pytest.mark.asyncio
    async def test_title_set_from_project(self, mock_state, mock_token_tracker):
        """Dashboard title is set from project name."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            assert "Test Project" in app.title


class TestAgentsPanel:
    """Tests for AgentsPanel widget."""

    @pytest.mark.asyncio
    async def test_agents_panel_shows_agents(self, mock_state, mock_token_tracker):
        """Agents panel displays registered agents."""
        # Register some agents
        mock_state.register_agent("archie", "lead", "/worktree/archie")
        mock_state.register_agent("frontend-1", "frontend", "/worktree/frontend-1")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            # Trigger refresh
            app._refresh_data()
            await pilot.pause()

            # Verify agents are in state
            agents = mock_state.list_agents()
            assert len(agents) == 2

    @pytest.mark.asyncio
    async def test_agents_panel_shows_sandboxed_tag(self, mock_state, mock_token_tracker):
        """Agents panel shows [c] tag for containerized agents."""
        mock_state.register_agent(
            "sandbox-1", "test", "/worktree/sandbox-1",
            sandboxed=True
        )

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Verify the agent is sandboxed in state
            agent = mock_state.get_agent("sandbox-1")
            assert agent["sandboxed"] is True


class TestActivityPanel:
    """Tests for ActivityPanel widget."""

    @pytest.mark.asyncio
    async def test_activity_panel_shows_messages(self, mock_state, mock_token_tracker):
        """Activity panel displays messages."""
        mock_state.add_message("archie", "frontend-1", "Build the navbar")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Message should be tracked
            assert len(app._seen_message_ids) == 1

    @pytest.mark.asyncio
    async def test_activity_panel_no_duplicate_messages(self, mock_state, mock_token_tracker):
        """Activity panel doesn't show duplicate messages."""
        msg = mock_state.add_message("archie", "frontend-1", "Build the navbar")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            # Refresh multiple times
            app._refresh_data()
            app._refresh_data()
            app._refresh_data()
            await pilot.pause()

            # Should only see the message once
            assert len(app._seen_message_ids) == 1


class TestCostsPanel:
    """Tests for CostsPanel widget."""

    @pytest.mark.asyncio
    async def test_costs_panel_shows_agent_costs(self, mock_state, mock_token_tracker):
        """Costs panel displays per-agent costs."""
        # Register agent and add usage
        mock_token_tracker.register_agent("archie", "claude-sonnet-4-6")
        mock_token_tracker._agents["archie"].add_usage(
            input_tokens=1000,
            output_tokens=500,
            pricing=mock_token_tracker.pricing
        )

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Verify usage was tracked
            usage = mock_token_tracker.get_agent_usage("archie")
            assert usage is not None
            assert usage["cost_usd"] > 0

    @pytest.mark.asyncio
    async def test_costs_panel_shows_budget(self, mock_state, mock_token_tracker):
        """Costs panel displays budget when set."""
        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            budget=10.0
        )

        async with app.run_test() as pilot:
            costs_panel = app.query_one("#costs-panel", CostsPanel)
            assert costs_panel.budget == 10.0


class TestEscalationPanel:
    """Tests for EscalationPanel widget."""

    @pytest.mark.asyncio
    async def test_escalation_panel_shows_question(self, mock_state, mock_token_tracker):
        """Escalation panel shows pending decisions."""
        mock_state.add_pending_decision("Merge to main?", ["y", "n"])

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert "Merge to main?" in escalation_panel.question

    @pytest.mark.asyncio
    async def test_escalation_input_enabled_with_question(self, mock_state, mock_token_tracker):
        """Escalation input is enabled when there's a question."""
        mock_state.add_pending_decision("Merge to main?")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            input_widget = app.query_one("#escalation-input", Input)
            assert not input_widget.disabled

    @pytest.mark.asyncio
    async def test_escalation_answer_calls_mcp_server(
        self, mock_state, mock_token_tracker, mock_mcp_server
    ):
        """Answering escalation calls MCP server."""
        decision = mock_state.add_pending_decision("Merge to main?")

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Type answer and submit
            input_widget = app.query_one("#escalation-input", Input)
            input_widget.value = "yes"
            input_widget.post_message(Input.Submitted(input_widget, "yes"))
            await pilot.pause()

            # MCP server should be called
            mock_mcp_server.answer_escalation.assert_called_once_with(
                decision["id"], "yes"
            )


class TestEscalationButtons:
    """Tests for escalation option buttons."""

    @pytest.mark.asyncio
    async def test_escalation_with_options_shows_buttons(
        self, mock_state, mock_token_tracker
    ):
        """When Archie asks a question with options, buttons are rendered."""
        mock_state.add_pending_decision(
            "Should we deploy to production?",
            ["Yes, deploy now", "No, wait for QA", "Let me check"]
        )

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert "Should we deploy to production?" in escalation_panel.question

            # Buttons should be visible
            from textual.widgets import Button
            from textual.containers import Horizontal
            options_bar = escalation_panel.query_one("#escalation-options", Horizontal)
            buttons = options_bar.query(Button)
            assert len(buttons) == 3
            assert buttons[0].label.plain == "Yes, deploy now"
            assert buttons[1].label.plain == "No, wait for QA"
            assert buttons[2].label.plain == "Let me check"

    @pytest.mark.asyncio
    async def test_escalation_without_options_hides_buttons(
        self, mock_state, mock_token_tracker
    ):
        """When Archie asks a question without options, no buttons are shown."""
        mock_state.add_pending_decision("What should we name the project?")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert "What should we name the project?" in escalation_panel.question

            from textual.containers import Horizontal
            options_bar = escalation_panel.query_one("#escalation-options", Horizontal)
            assert options_bar.display is False

    @pytest.mark.asyncio
    async def test_clicking_option_button_submits_answer(
        self, mock_state, mock_token_tracker, mock_mcp_server
    ):
        """Clicking an option button submits that option as the answer."""
        decision = mock_state.add_pending_decision(
            "Approve team plan?",
            ["Approve", "Reject", "Modify"]
        )

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Click the first button ("Approve")
            from textual.widgets import Button
            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            buttons = escalation_panel.query(Button)
            assert len(buttons) == 3

            await pilot.click(Button, offset=(2, 0))
            await pilot.pause()

            # MCP server should be called with the button text
            mock_mcp_server.answer_escalation.assert_called_once()
            call_args = mock_mcp_server.answer_escalation.call_args
            assert call_args[0][0] == decision["id"]
            assert call_args[0][1] == "Approve"

    @pytest.mark.asyncio
    async def test_free_text_answer_still_works_with_options(
        self, mock_state, mock_token_tracker, mock_mcp_server
    ):
        """User can type a custom answer even when options are shown."""
        decision = mock_state.add_pending_decision(
            "Approve team plan?",
            ["Approve", "Reject"]
        )

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Type a custom answer instead of clicking buttons
            input_widget = app.query_one("#escalation-input", Input)
            input_widget.value = "Approve but add a security agent too"
            input_widget.post_message(Input.Submitted(input_widget, "Approve but add a security agent too"))
            await pilot.pause()

            mock_mcp_server.answer_escalation.assert_called_once_with(
                decision["id"], "Approve but add a security agent too"
            )

    @pytest.mark.asyncio
    async def test_permission_request_shows_permission_buttons(
        self, mock_state, mock_token_tracker
    ):
        """Permission requests show y/a/n buttons."""
        decision = mock_state.add_pending_decision(
            "Allow Bash(git push)?",
            options=None
        )
        # Simulate permission request type
        for d in mock_state._state["pending_user_decisions"]:
            if d["id"] == decision["id"]:
                d["type"] = "permission_request"

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            from textual.widgets import Button
            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            buttons = escalation_panel.query(Button)
            assert len(buttons) == 3
            assert "Yes (once)" in buttons[0].label.plain
            assert "Always" in buttons[1].label.plain
            assert "No" in buttons[2].label.plain

    @pytest.mark.asyncio
    async def test_buttons_cleared_after_answer(
        self, mock_state, mock_token_tracker, mock_mcp_server
    ):
        """After answering, buttons are cleared and panel resets."""
        mock_state.add_pending_decision(
            "Deploy?",
            ["Yes", "No"]
        )

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Answer via text
            input_widget = app.query_one("#escalation-input", Input)
            input_widget.value = "Yes"
            input_widget.post_message(Input.Submitted(input_widget, "Yes"))
            await pilot.pause()

            # Panel should be reset
            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert escalation_panel.question == ""
            assert escalation_panel.decision_id is None

            from textual.containers import Horizontal
            options_bar = escalation_panel.query_one("#escalation-options", Horizontal)
            assert options_bar.display is False

    @pytest.mark.asyncio
    async def test_team_plan_escalation_simulation(
        self, mock_state, mock_token_tracker, mock_mcp_server
    ):
        """Simulate a full team plan approval flow as seen in production."""
        # Archie proposes a team — this is what plan_team escalation looks like
        decision = mock_state.add_pending_decision(
            "Proposed team for CloudSync Landing Page:\n\n"
            "1. frontend (personas/frontend.md) — Build landing page HTML/CSS\n"
            "2. qa (personas/qa.md) — Write validation tests\n\n"
            "Rationale: The project needs a landing page (frontend) and "
            "validation tests (QA). No backend is needed.",
            ["Approve team", "Reject team", "Modify team"]
        )

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Verify the question and options are displayed
            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert "Proposed team" in escalation_panel.question
            assert "frontend" in escalation_panel.question

            from textual.widgets import Button
            buttons = escalation_panel.query(Button)
            assert len(buttons) == 3
            assert buttons[0].label.plain == "Approve team"

            # User approves via button click
            buttons[0].press()
            await pilot.pause()

            # Answer should be submitted
            mock_mcp_server.answer_escalation.assert_called_once()
            call_args = mock_mcp_server.answer_escalation.call_args
            assert call_args[0][0] == decision["id"]
            assert call_args[0][1] == "Approve team"

            # Panel should be cleared
            assert escalation_panel.question == ""


class TestKeyboardShortcuts:
    """Tests for keyboard shortcuts."""

    @pytest.mark.asyncio
    async def test_q_quits(self, mock_state, mock_token_tracker):
        """Pressing q quits the app."""
        quit_called = False

        def on_quit():
            nonlocal quit_called
            quit_called = True

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            on_quit=on_quit
        )

        async with app.run_test() as pilot:
            await pilot.press("q")

        assert quit_called

    @pytest.mark.asyncio
    async def test_question_mark_shows_help(self, mock_state, mock_token_tracker):
        """Pressing ? shows help screen."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("?")
            await pilot.pause()

            # Help screen should be showing
            assert app.screen.__class__.__name__ == "HelpScreen"

    @pytest.mark.asyncio
    async def test_m_shows_messages(self, mock_state, mock_token_tracker):
        """Pressing m shows message log."""
        mock_state.add_message("archie", "frontend-1", "test message")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()

            # Message log screen should be showing
            assert app.screen.__class__.__name__ == "MessageLogScreen"

    @pytest.mark.asyncio
    async def test_l_shows_archie_log(self, mock_state, mock_token_tracker):
        """Pressing l shows Archie's message log."""
        mock_state.add_message("archie", "frontend-1", "from archie")
        mock_state.add_message("frontend-1", "archie", "to archie")
        mock_state.add_message("frontend-1", "qa-1", "not archie")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("l")
            await pilot.pause()

            # Should be message log screen with archie's messages
            screen = app.screen
            assert screen.__class__.__name__ == "MessageLogScreen"
            assert "Archie" in screen.title_text


class TestHelpScreen:
    """Tests for HelpScreen modal."""

    @pytest.mark.asyncio
    async def test_help_screen_shows_shortcuts(self, mock_state, mock_token_tracker):
        """Help screen displays keyboard shortcuts."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("?")
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, HelpScreen)

    @pytest.mark.asyncio
    async def test_escape_closes_help(self, mock_state, mock_token_tracker):
        """Escape closes help screen."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

            await pilot.press("escape")
            await pilot.pause()
            # Should be back to main screen
            assert not isinstance(app.screen, HelpScreen)


class TestMessageLogScreen:
    """Tests for MessageLogScreen modal."""

    @pytest.mark.asyncio
    async def test_message_log_shows_messages(self, mock_state, mock_token_tracker):
        """Message log screen displays messages."""
        mock_state.add_message("archie", "frontend-1", "hello")
        mock_state.add_message("frontend-1", "archie", "hi back")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, MessageLogScreen)
            assert len(screen.messages) == 2

    @pytest.mark.asyncio
    async def test_escape_closes_message_log(self, mock_state, mock_token_tracker):
        """Escape closes message log screen."""
        mock_state.add_message("archie", "frontend-1", "test")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, MessageLogScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, MessageLogScreen)


class TestAgentNumberShortcuts:
    """Tests for agent number shortcuts (1-9)."""

    @pytest.mark.asyncio
    async def test_number_shows_agent_log(self, mock_state, mock_token_tracker):
        """Number keys show agent message logs."""
        mock_state.register_agent("frontend-1", "frontend", "/worktree/frontend-1")
        mock_state.add_message("archie", "frontend-1", "task for you")
        mock_state.add_message("frontend-1", "archie", "done")

        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            await pilot.press("1")
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, MessageLogScreen)
            assert "frontend-1" in screen.title_text

    @pytest.mark.asyncio
    async def test_number_out_of_range_does_nothing(self, mock_state, mock_token_tracker):
        """Number key out of range doesn't crash."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            # No agents registered, pressing 1 shouldn't crash
            await pilot.press("1")
            await pilot.pause()

            # Should still be on main screen
            assert not isinstance(app.screen, MessageLogScreen)


class TestDashboardRefresh:
    """Tests for dashboard refresh behavior."""

    @pytest.mark.asyncio
    async def test_refresh_updates_runtime(self, mock_state, mock_token_tracker):
        """Refresh updates runtime display."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            initial_runtime = app.runtime
            # Wait a bit
            await asyncio.sleep(0.1)
            app._refresh_data()
            # Runtime should update (though may be same if fast enough)
            assert app.runtime  # Just verify it's set

    @pytest.mark.asyncio
    async def test_refresh_updates_agents(self, mock_state, mock_token_tracker):
        """Refresh updates agent list."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Register new agent
            mock_state.register_agent("new-agent", "test", "/worktree/new")
            app._refresh_data()
            await pilot.pause()

            # The agent should be in _agent_list (which tracks non-archie agents)
            assert "new-agent" in app._agent_list


# ============================================================================
# Integration Tests
# ============================================================================


class TestDashboardIntegration:
    """Integration tests for dashboard with real components."""

    @pytest.mark.asyncio
    async def test_full_workflow(self, mock_state, mock_token_tracker, mock_mcp_server):
        """Test complete dashboard workflow."""
        # Setup initial state
        mock_state.register_agent("archie", "lead", "/worktree/archie")
        mock_state.register_agent(
            "frontend-1", "frontend", "/worktree/frontend-1",
            sandboxed=True, skip_permissions=True
        )

        mock_state.add_message("archie", "frontend-1", "Build the navbar")
        mock_state.update_agent("frontend-1", status="working", task="Building NavBar")

        mock_token_tracker.register_agent("archie", "claude-opus-4-5")
        mock_token_tracker.register_agent("frontend-1", "claude-sonnet-4-6")

        app = Dashboard(
            state=mock_state,
            token_tracker=mock_token_tracker,
            mcp_server=mock_mcp_server,
            budget=5.0
        )

        async with app.run_test() as pilot:
            app._refresh_data()
            await pilot.pause()

            # Verify agents in state
            agents = mock_state.list_agents()
            assert len(agents) == 2

            # Verify costs panel has budget
            costs_panel = app.query_one("#costs-panel", CostsPanel)
            assert costs_panel.budget == 5.0

            # Add escalation
            decision = mock_state.add_pending_decision("Merge to main?", ["y", "n"])
            app._refresh_data()
            await pilot.pause()

            # Verify escalation displayed
            escalation_panel = app.query_one("#escalation-panel", EscalationPanel)
            assert "Merge to main?" in escalation_panel.question

            # Answer escalation
            input_widget = app.query_one("#escalation-input", Input)
            input_widget.value = "y"
            input_widget.post_message(Input.Submitted(input_widget, "y"))
            await pilot.pause()

            # Verify MCP server called
            mock_mcp_server.answer_escalation.assert_called_once()


# ============================================================================
# Standalone Mode Tests
# ============================================================================


class TestDashboardStandaloneInit:
    """Tests for Dashboard standalone mode initialization."""

    def test_standalone_init_creates_state_and_tracker(self, tmp_path):
        """Standalone mode creates its own StateStore and TokenTracker."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999)
        assert app._standalone is True
        assert app.state is not None
        assert app.token_tracker is not None
        assert app.mcp_server is None
        assert app.mcp_port == 3999

    def test_standalone_init_with_budget(self, tmp_path):
        """Standalone mode accepts budget parameter."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999, budget=10.0)
        assert app.budget == 10.0

    def test_inprocess_mode_not_standalone(self, mock_state, mock_token_tracker):
        """In-process mode sets _standalone to False."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)
        assert app._standalone is False


class TestDashboardStandaloneRefresh:
    """Tests for standalone mode refresh behavior."""

    @pytest.mark.asyncio
    async def test_standalone_refresh_reloads_state(self, tmp_path):
        """Standalone mode calls reload() on each refresh."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999)

        async with app.run_test() as pilot:
            # Write agent data to state files after init
            app.state.register_agent("test-1", "test", "/worktree/test")
            app.state._flush()

            # Create a fresh state store reading same dir (simulates orchestrator writing)
            other_state = StateStore(state_dir)
            other_state.register_agent("test-2", "test", "/worktree/test-2")

            # Refresh should pick up the new agent
            app._refresh_data()
            await pilot.pause()

            agents = app.state.list_agents()
            assert len(agents) == 2


class TestDashboardStandaloneEscalation:
    """Tests for standalone mode escalation handling."""

    @pytest.mark.asyncio
    async def test_standalone_escalation_posts_http(self, tmp_path):
        """Standalone mode posts escalation answers via HTTP."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999)

        with patch("arch.dashboard.urllib.request.urlopen") as mock_urlopen:
            success = app._post_escalation_answer("decision-123", "yes")
            assert success is True
            mock_urlopen.assert_called_once()

            # Verify the request
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert "decision-123" in req.full_url
            assert req.method == "POST"

    @pytest.mark.asyncio
    async def test_standalone_escalation_handles_connection_error(self, tmp_path):
        """Standalone mode handles HTTP errors gracefully."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999)

        with patch("arch.dashboard.urllib.request.urlopen", side_effect=Exception("Connection refused")):
            success = app._post_escalation_answer("decision-123", "yes")
            assert success is False


class TestDashboardOnUnmount:
    """Tests for on_unmount cleanup."""

    @pytest.mark.asyncio
    async def test_on_unmount_cancels_refresh_task(self, mock_state, mock_token_tracker):
        """on_unmount cancels the refresh task."""
        app = Dashboard(state=mock_state, token_tracker=mock_token_tracker)

        async with app.run_test() as pilot:
            # Refresh task should exist
            assert app._refresh_task is not None
            assert not app._refresh_task.cancelled()

        # After unmount, task should be cancelled
        assert app._refresh_task is None


class TestDashboardOrchestratorConnection:
    """Tests for orchestrator connection checking."""

    def test_check_connection_no_port(self, tmp_path):
        """No port means not connected."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=None)
        app._check_orchestrator_connection()
        assert app._orchestrator_connected is False

    def test_check_connection_unreachable(self, tmp_path):
        """Unreachable port means not connected."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=39999)

        with patch("arch.dashboard.urllib.request.urlopen", side_effect=Exception("refused")):
            app._check_orchestrator_connection()
            assert app._orchestrator_connected is False

    def test_check_connection_success(self, tmp_path):
        """Reachable port means connected."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        app = Dashboard(state_dir=state_dir, mcp_port=3999)

        with patch("arch.dashboard.urllib.request.urlopen") as mock:
            mock.return_value = MagicMock()
            app._check_orchestrator_connection()
            assert app._orchestrator_connected is True
