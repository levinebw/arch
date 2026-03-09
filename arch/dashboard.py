"""
ARCH Dashboard

Textual TUI dashboard for monitoring and interacting with ARCH.
Displays agent status, activity log, costs, and handles user escalations.

Can run in two modes:
- In-process: receives StateStore/TokenTracker/MCPServer objects directly (tests)
- Standalone: reads from state directory files, posts escalations via HTTP (production)
"""

from __future__ import annotations

import asyncio
import json as json_mod
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)
from rich.text import Text

if TYPE_CHECKING:
    from arch.mcp_server import MCPServer
    from arch.state import StateStore
    from arch.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

# Refresh interval in seconds
REFRESH_INTERVAL = 2.0

# Status indicator symbols and colors
# Note: Rich uses "bright_black" for gray color
STATUS_INDICATORS = {
    "working": ("●", "green"),
    "blocked": ("●", "yellow"),
    "waiting_review": ("●", "yellow"),
    "idle": ("○", "bright_black"),
    "done": ("✓", "green"),
    "error": ("✗", "red"),
}


def format_runtime(start_time: Optional[str]) -> str:
    """Format runtime duration as HH:MM:SS."""
    if not start_time:
        return "00:00:00"

    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except (ValueError, TypeError):
        return "00:00:00"


def format_timestamp(ts: Optional[str]) -> str:
    """Format ISO timestamp as HH:MM."""
    if not ts:
        return "--:--"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, TypeError, AttributeError):
        return "--:--"


def format_agent_display(agent: dict[str, Any]) -> Text:
    """Format an agent for display."""
    status = agent.get("status", "idle")
    symbol, color = STATUS_INDICATORS.get(status, ("○", "bright_black"))

    agent_id = agent.get("id", "unknown")
    task = agent.get("task", "")

    # Add tags
    tags = []
    if agent.get("sandboxed"):
        tags.append("[c]")
    if agent.get("skip_permissions"):
        tags.append("[!]")

    tag_str = "".join(tags)

    # Build rich text
    text = Text()
    text.append(symbol, style=color)
    if tag_str:
        text.append(tag_str, style="cyan")
    text.append(f" {agent_id}")

    # Add task on second line if present
    if task:
        display_task = task[:20] + "..." if len(task) > 20 else task
        text.append(f"\n  {display_task}", style="dim")

    return text


class AgentsPanel(Static):
    """Panel showing all active agents."""

    def compose(self) -> ComposeResult:
        yield Static("AGENTS", classes="panel-title")
        yield Static("", id="agents-content")

    def update_agents(self, agents: list[dict[str, Any]]) -> None:
        """Update the agents display."""
        content = self.query_one("#agents-content", Static)
        if not agents:
            content.update("No agents")
            return

        text = Text()
        for i, agent in enumerate(agents):
            if i > 0:
                text.append("\n")
            text.append_text(format_agent_display(agent))

        content.update(text)


class ActivityPanel(Static):
    """Panel showing activity log (messages)."""

    def compose(self) -> ComposeResult:
        yield Static("ACTIVITY LOG", classes="panel-title")
        yield RichLog(id="activity-log", highlight=True, markup=True)

    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message to the activity log."""
        log = self.query_one("#activity-log", RichLog)

        ts = format_timestamp(message.get("timestamp", ""))
        sender = message.get("from", "?")
        content = message.get("content", "")

        # Color messages by type
        if "[stderr]" in content:
            stderr_text = content.replace("[stderr] ", "")
            log.write(f"[dim]{ts} {sender:10} {stderr_text}[/dim]")
        elif "BLOCKED" in content.upper():
            log.write(f"[yellow]{ts} {sender:10} {content}[/yellow]")
        else:
            log.write(f"{ts} {sender:10} {content}")


class CostsPanel(Static):
    """Panel showing per-agent costs and budget."""

    budget: Optional[float] = None

    def compose(self) -> ComposeResult:
        yield Static("COSTS", classes="panel-title")
        yield Static("", id="costs-content")
        yield ProgressBar(id="costs-bar", total=100, show_eta=False)

    def update_costs(self, costs: dict[str, dict[str, Any]]) -> None:
        """Update the costs display."""
        content = self.query_one("#costs-content", Static)
        bar = self.query_one("#costs-bar", ProgressBar)

        lines = []
        total = 0.0

        for agent_id, usage in costs.items():
            cost = usage.get("cost_usd", 0.0)
            total += cost
            lines.append(f"{agent_id:12} ${cost:.2f}")

        lines.append("─" * 14)
        lines.append(f"{'Total':12} ${total:.2f}")

        if self.budget:
            lines.append(f"{'Budget':12} ${self.budget:.2f}")
            pct = min(100, (total / self.budget) * 100) if self.budget > 0 else 0
            bar.update(progress=pct)

            # Update bar style based on percentage
            bar.remove_class("danger", "warning", "normal")
            if pct >= 90:
                bar.add_class("danger")
            elif pct >= 75:
                bar.add_class("warning")
            else:
                bar.add_class("normal")
        else:
            bar.update(progress=0)

        content.update("\n".join(lines))


class EscalationPanel(Container):
    """Panel for displaying and answering escalations."""

    question: reactive[str] = reactive("")
    options: reactive[list[str]] = reactive([])
    decision_id: reactive[Optional[str]] = reactive(None)
    is_permission_request: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Static("", id="escalation-question")
        yield Horizontal(id="escalation-options")
        yield Input(placeholder="Send a message to Archie...", id="escalation-input")

    def watch_question(self, question: str) -> None:
        """Update the question display."""
        q_widget = self.query_one("#escalation-question", Static)
        input_widget = self.query_one("#escalation-input", Input)
        options_bar = self.query_one("#escalation-options", Horizontal)

        # Clear old option buttons
        options_bar.remove_children()

        if question:
            if self.is_permission_request:
                display = f"🔐 PERMISSION REQUEST:\n{question}"
                input_widget.placeholder = "Type y, a, or n..."
                options_bar.mount(Button("[y]es once", id="opt-y", variant="success"))
                options_bar.mount(Button("[a]lways", id="opt-a", variant="warning"))
                options_bar.mount(Button("[n]o", id="opt-n", variant="error"))
                options_bar.display = True
            else:
                display = f"⚠ ARCHIE ASKS: {question}"
                if self.options:
                    for i, opt in enumerate(self.options):
                        options_bar.mount(Button(opt, id=f"opt-{i}", variant="primary"))
                    options_bar.display = True
                else:
                    options_bar.display = False
                input_widget.placeholder = "Or type a custom answer..."

            q_widget.update(display)
            input_widget.focus()
        else:
            q_widget.update("")
            input_widget.value = ""
            input_widget.placeholder = "Send a message to Archie..."
            options_bar.display = False


class HelpScreen(ModalScreen[None]):
    """Modal screen showing keyboard shortcuts."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("?", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Static("ARCH Dashboard Help", classes="help-title"),
            Static(""),
            Static("Keyboard Shortcuts:", classes="help-section"),
            Static("  q         Quit (graceful shutdown)"),
            Static("  ?         Show this help"),
            Static("  l         View Archie's conversation log"),
            Static("  1-9       View agent conversation logs"),
            Static("  m         View message bus log"),
            Static("  c         Toggle costs panel"),
            Static("  e         View MCP tool call events"),
            Static("  Escape    Close modals"),
            Static(""),
            Static("Input Bar:", classes="help-section"),
            Static("  Type + Enter   Send message to Archie"),
            Static("  (when escalation is pending, answers it instead)"),
            Static(""),
            Static("Press ? or Escape to close", classes="help-footer"),
            id="help-container",
        )


class MessageLogScreen(ModalScreen[None]):
    """Modal screen showing full message log."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, messages: list[dict[str, Any]], title: str = "Messages") -> None:
        super().__init__()
        self.messages = messages
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self.title_text, classes="modal-title"),
            RichLog(id="full-log", highlight=True),
            Static("Press Escape to close", classes="modal-footer"),
            id="log-container",
        )

    def on_mount(self) -> None:
        """Populate the log when mounted."""
        log = self.query_one("#full-log", RichLog)
        for msg in self.messages:
            ts = format_timestamp(msg.get("timestamp", ""))
            sender = msg.get("from", "?")
            recipient = msg.get("to", "?")
            content = msg.get("content", "")
            log.write(f"[{ts}] {sender} → {recipient}: {content}")


class EventLogScreen(ModalScreen[None]):
    """Modal screen showing MCP tool call event history."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, events: list[dict[str, Any]], title: str = "MCP Events") -> None:
        super().__init__()
        self.events = events
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self.title_text, classes="modal-title"),
            RichLog(id="event-log", highlight=True),
            Static("Press Escape to close", classes="modal-footer"),
            id="log-container",
        )

    def on_mount(self) -> None:
        """Populate the event log."""
        log = self.query_one("#event-log", RichLog)
        for evt in self.events:
            ts = format_timestamp(evt.get("timestamp", ""))
            agent = evt.get("agent_id", "?")
            tool = evt.get("tool", "?")
            duration = evt.get("duration_ms", 0)
            result = evt.get("result", {})
            status = result.get("status", "?") if isinstance(result, dict) else "ok"
            args = evt.get("args", {})

            # Status indicator
            icon = "+" if status == "ok" else "!"

            # Format args compactly
            args_parts = []
            for k, v in args.items():
                if isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "..."
                args_parts.append(f"{k}={v}")
            args_str = ", ".join(args_parts) if args_parts else ""

            # Main line
            log.write(f"[{ts}] {icon} {agent:12} {tool}({args_str})")

            # Detail line for errors or interesting results
            if status == "error":
                detail = result.get("detail", "")
                log.write(f"             ERROR: {detail}")
            elif duration > 1000:
                log.write(f"             ({duration:.0f}ms)")


class Dashboard(App):
    """
    Main ARCH Dashboard application.

    Displays agent status, activity log, costs, and handles user escalations.

    Two modes:
    - In-process: pass state/token_tracker/mcp_server objects directly
    - Standalone: pass state_dir and mcp_port to read from files + HTTP
    """

    CSS = """
    /* Main layout */
    #main-container {
        layout: horizontal;
        height: 1fr;
    }

    #agents-panel {
        width: 22;
        height: 100%;
        border: solid green;
        padding: 0 1;
    }

    #activity-panel {
        width: 1fr;
        height: 100%;
        border: solid blue;
        padding: 0 1;
    }

    #costs-panel {
        width: 20;
        height: 100%;
        display: none;
        border: solid yellow;
        padding: 0 1;
    }

    #escalation-panel {
        height: auto;
        max-height: 12;
        border: solid red;
        padding: 0 1;
    }

    .panel-title {
        text-style: bold;
        color: white;
        height: 1;
    }

    #agents-content {
        height: auto;
    }

    #costs-content {
        height: auto;
    }

    #activity-log {
        height: 1fr;
    }

    /* Progress bar colors */
    ProgressBar.normal Bar {
        color: green;
    }

    ProgressBar.warning Bar {
        color: yellow;
    }

    ProgressBar.danger Bar {
        color: red;
    }

    #costs-bar {
        height: 1;
        margin-top: 1;
    }

    /* Help screen */
    HelpScreen {
        align: center middle;
    }

    #help-container {
        width: 50;
        height: auto;
        padding: 1 2;
        border: solid green;
        background: $surface;
    }

    .help-title {
        text-style: bold;
        text-align: center;
    }

    .help-section {
        text-style: bold;
        margin-top: 1;
    }

    .help-footer {
        text-align: center;
        margin-top: 1;
        color: $text-muted;
    }

    /* Log screen */
    MessageLogScreen {
        align: center middle;
    }

    #log-container {
        width: 80%;
        height: 80%;
        padding: 1;
        border: solid blue;
        background: $surface;
    }

    .modal-title {
        text-style: bold;
        text-align: center;
        height: 1;
    }

    .modal-footer {
        text-align: center;
        color: $text-muted;
        height: 1;
    }

    #full-log {
        height: 1fr;
        border: solid $primary;
    }

    /* Input styling */
    #escalation-input {
        height: 3;
    }

    #escalation-question {
        height: auto;
        max-height: 4;
    }

    #escalation-options {
        height: auto;
        display: none;
    }

    #escalation-options Button {
        min-width: 12;
        margin: 0 1 0 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("l", "view_archie_log", "Archie Log"),
        Binding("m", "view_messages", "Messages"),
        Binding("c", "toggle_costs", "Costs"),
        Binding("e", "view_events", "Events"),
        Binding("1", "view_agent_1", "Agent 1", show=False),
        Binding("2", "view_agent_2", "Agent 2", show=False),
        Binding("3", "view_agent_3", "Agent 3", show=False),
        Binding("4", "view_agent_4", "Agent 4", show=False),
        Binding("5", "view_agent_5", "Agent 5", show=False),
        Binding("6", "view_agent_6", "Agent 6", show=False),
        Binding("7", "view_agent_7", "Agent 7", show=False),
        Binding("8", "view_agent_8", "Agent 8", show=False),
        Binding("9", "view_agent_9", "Agent 9", show=False),
    ]

    project_name: reactive[str] = reactive("ARCH")
    runtime: reactive[str] = reactive("00:00:00")

    def __init__(
        self,
        state: Optional["StateStore"] = None,
        token_tracker: Optional["TokenTracker"] = None,
        mcp_server: Optional["MCPServer"] = None,
        budget: Optional[float] = None,
        on_quit: Optional[Callable[[], None]] = None,
        # Standalone mode params
        state_dir: Optional[Path] = None,
        mcp_port: Optional[int] = None,
    ) -> None:
        """
        Initialize the dashboard.

        In-process mode: pass state, token_tracker, mcp_server directly.
        Standalone mode: pass state_dir and mcp_port to read from files + HTTP.
        """
        super().__init__()
        self.budget = budget
        self.on_quit_callback = on_quit

        if state_dir is not None:
            # Standalone mode: read from files, post escalations via HTTP
            from arch.state import StateStore
            from arch.token_tracker import TokenTracker

            self.state = StateStore(state_dir)
            self.token_tracker = TokenTracker(state_dir=state_dir)
            self.mcp_server = None
            self.mcp_port = mcp_port
            self._standalone = True
        else:
            # In-process mode: use provided objects directly
            self.state = state
            self.token_tracker = token_tracker
            self.mcp_server = mcp_server
            self.mcp_port = None
            self._standalone = False

        # Track seen messages to avoid duplicates
        self._seen_message_ids: set[str] = set()

        # Track agents for number key shortcuts
        self._agent_list: list[str] = []

        # Refresh task
        self._refresh_task: Optional[asyncio.Task] = None

        # Orchestrator connection status (standalone mode)
        self._orchestrator_connected = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            AgentsPanel(id="agents-panel"),
            ActivityPanel(id="activity-panel"),
            CostsPanel(id="costs-panel"),
            id="main-container",
        )
        yield EscalationPanel(id="escalation-panel")
        yield Footer()

    def on_mount(self) -> None:
        """Start the refresh loop when mounted."""
        # Set initial project name from state
        project = self.state.get_project()
        self.project_name = project.get("name", "ARCH")
        self.title = f"ARCH · {self.project_name}"

        # Set budget on costs panel
        costs_panel = self.query_one("#costs-panel", CostsPanel)
        costs_panel.budget = self.budget

        # Start refresh loop
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        # Initial refresh
        self._refresh_data()

    def on_unmount(self) -> None:
        """Cancel refresh task on unmount."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        """Periodically refresh dashboard data."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            try:
                self._refresh_data()
            except Exception as e:
                logger.debug(f"Dashboard refresh error: {e}")

    def _refresh_data(self) -> None:
        """Refresh all dashboard data from state."""
        # In standalone mode, reload state from disk
        if self._standalone:
            self.state.reload()
            self.token_tracker._load()
            self._check_orchestrator_connection()

        # Update runtime
        project = self.state.get_project()
        self.runtime = format_runtime(project.get("started_at"))

        # Check for project completion
        if project.get("status") == "complete":
            summary = project.get("summary", "All tasks complete.")
            self.sub_title = f"COMPLETE — {summary} — Press q to exit"
        elif self._standalone:
            conn = "Connected" if self._orchestrator_connected else "Not connected"
            self.sub_title = f"Runtime: {self.runtime} · {conn}"
        else:
            self.sub_title = f"Runtime: {self.runtime}"

        # Update agents
        agents = self.state.list_agents()
        agents_panel = self.query_one("#agents-panel", AgentsPanel)
        agents_panel.update_agents(agents)

        # Track agent IDs for number shortcuts (excluding archie)
        self._agent_list = [a["id"] for a in agents if a["id"] != "archie"]

        # Update costs
        costs = self.token_tracker.get_all_usage()
        costs_panel = self.query_one("#costs-panel", CostsPanel)
        costs_panel.update_costs(costs)

        # Update activity log with new messages
        messages = self.state.get_all_messages()
        activity_panel = self.query_one("#activity-panel", ActivityPanel)

        for msg in messages:
            msg_id = msg.get("id")
            if msg_id and msg_id not in self._seen_message_ids:
                self._seen_message_ids.add(msg_id)
                activity_panel.add_message(msg)

        # Check for pending decisions
        decisions = self.state.get_pending_decisions()
        escalation_panel = self.query_one("#escalation-panel", EscalationPanel)

        if decisions:
            decision = decisions[0]  # Handle one at a time
            escalation_panel.decision_id = decision.get("id")
            escalation_panel.is_permission_request = decision.get("type") == "permission_request"
            escalation_panel.question = decision.get("question", "")
            escalation_panel.options = decision.get("options", [])
        else:
            escalation_panel.decision_id = None
            escalation_panel.is_permission_request = False
            escalation_panel.question = ""
            escalation_panel.options = []

    def _check_orchestrator_connection(self) -> None:
        """Check if the orchestrator is reachable (standalone mode)."""
        if not self.mcp_port:
            self._orchestrator_connected = False
            return

        try:
            url = f"http://127.0.0.1:{self.mcp_port}/api/health"
            req = urllib.request.Request(url, method="GET")
            urllib.request.urlopen(req, timeout=1)
            self._orchestrator_connected = True
        except Exception:
            self._orchestrator_connected = False

    def _post_escalation_answer(self, decision_id: str, answer: str) -> bool:
        """Post an escalation answer via HTTP to the MCP server."""
        if not self.mcp_port:
            return False

        url = f"http://127.0.0.1:{self.mcp_port}/api/escalation/{decision_id}"
        data = json_mod.dumps({"answer": answer}).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            logger.warning(f"Failed to post escalation answer: {e}")
            return False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle input submission — either answer escalation or send message to Archie."""
        if not event.value:
            return

        escalation_panel = self.query_one("#escalation-panel", EscalationPanel)
        decision_id = escalation_panel.decision_id

        if decision_id:
            # Answering a pending escalation
            self._submit_escalation_answer(event.value, escalation_panel, decision_id)
            event.input.value = ""
        else:
            # Sending a message to Archie
            self._submit_message_to_archie(event)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle option button clicks for escalations."""
        escalation_panel = self.query_one("#escalation-panel", EscalationPanel)
        decision_id = escalation_panel.decision_id
        if not decision_id:
            return

        button_id = event.button.id or ""
        if escalation_panel.is_permission_request:
            answer_map = {"opt-y": "yes (this time)", "opt-a": "always (this session)", "opt-n": "no"}
            answer = answer_map.get(button_id, event.button.label.plain)
        else:
            answer = event.button.label.plain

        self._submit_escalation_answer(answer, escalation_panel, decision_id)

    def _submit_escalation_answer(
        self, answer: str, panel: EscalationPanel, decision_id: str
    ) -> None:
        """Submit an answer to a pending escalation."""
        # For permission requests from typed input, expand short answers
        if panel.is_permission_request:
            answer_lower = answer.lower().strip()
            if answer_lower in ("y", "yes"):
                answer = "yes (this time)"
            elif answer_lower in ("a", "always"):
                answer = "always (this session)"
            elif answer_lower in ("n", "no"):
                answer = "no"

        # Answer the escalation
        if self._standalone:
            if not self._post_escalation_answer(decision_id, answer):
                activity_panel = self.query_one("#activity-panel", ActivityPanel)
                activity_panel.add_message({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "from": "dashboard",
                    "content": "[stderr] Failed to send answer (orchestrator not connected)",
                })
                return
        elif self.mcp_server:
            self.mcp_server.answer_escalation(decision_id, answer)

        # Clear the panel state
        panel.decision_id = None
        panel.is_permission_request = False
        panel.question = ""
        panel.options = []

        # Log the response
        activity_panel = self.query_one("#activity-panel", ActivityPanel)
        activity_panel.add_message({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from": "user",
            "content": f"Answered: {answer}",
        })

    def _submit_message_to_archie(self, event: Input.Submitted) -> None:
        """Send a user message to Archie via the state store."""
        message = event.value
        event.input.value = ""

        self.state.add_message(
            from_agent="user",
            to_agent="archie",
            content=message
        )

        # Log in activity panel
        activity_panel = self.query_one("#activity-panel", ActivityPanel)
        activity_panel.add_message({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from": "user",
            "content": f"Message to Archie: {message}",
        })

    def action_quit(self) -> None:
        """Handle quit action."""
        if self.on_quit_callback:
            self.on_quit_callback()
        self.exit()

    def action_help(self) -> None:
        """Show help screen."""
        self.push_screen(HelpScreen())

    def action_view_archie_log(self) -> None:
        """View Archie's message log (messages sent by archie)."""
        messages = [
            m for m in self.state.get_all_messages()
            if m.get("from") == "archie"
        ]
        self.push_screen(MessageLogScreen(messages, "Archie Messages"))

    def action_view_messages(self) -> None:
        """View full message log."""
        messages = self.state.get_all_messages()
        self.push_screen(MessageLogScreen(messages, "All Messages"))

    def action_toggle_costs(self) -> None:
        """Toggle costs panel visibility."""
        costs_panel = self.query_one("#costs-panel", CostsPanel)
        costs_panel.display = not costs_panel.display

    def action_view_events(self) -> None:
        """View MCP tool call event history."""
        events = self._load_events()
        self.push_screen(EventLogScreen(events, "MCP Events"))

    def _load_events(self) -> list[dict[str, Any]]:
        """Load events from events.jsonl."""
        events_path = Path(self.state.state_dir) / "events.jsonl"
        if not events_path.exists():
            return []
        events = []
        try:
            with open(events_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json_mod.loads(line))
        except (json_mod.JSONDecodeError, IOError):
            pass
        return events

    def _view_agent_log(self, index: int) -> None:
        """View an agent's message log by index."""
        if index < len(self._agent_list):
            agent_id = self._agent_list[index]
            messages = [
                m for m in self.state.get_all_messages()
                if m.get("from") == agent_id
            ]
            self.push_screen(MessageLogScreen(messages, f"{agent_id} Messages"))

    def action_view_agent_1(self) -> None:
        self._view_agent_log(0)

    def action_view_agent_2(self) -> None:
        self._view_agent_log(1)

    def action_view_agent_3(self) -> None:
        self._view_agent_log(2)

    def action_view_agent_4(self) -> None:
        self._view_agent_log(3)

    def action_view_agent_5(self) -> None:
        self._view_agent_log(4)

    def action_view_agent_6(self) -> None:
        self._view_agent_log(5)

    def action_view_agent_7(self) -> None:
        self._view_agent_log(6)

    def action_view_agent_8(self) -> None:
        self._view_agent_log(7)

    def action_view_agent_9(self) -> None:
        self._view_agent_log(8)
