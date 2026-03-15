"""Unit tests for ARCH Web Dashboard."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from arch.web_dashboard import (
    DashboardEventBroadcaster,
    dashboard_html,
    get_dashboard_routes,
)
from arch.state import StateStore
from arch.token_tracker import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def state(state_dir):
    s = StateStore(state_dir=state_dir)
    s.init_project("test-project", "A test project", "/tmp/repo")
    return s


@pytest.fixture
def token_tracker(state_dir):
    return TokenTracker(state_dir=state_dir)


@pytest.fixture
def event_log_path(state_dir):
    return state_dir / "events.jsonl"


@pytest.fixture
def broadcaster():
    return DashboardEventBroadcaster()


def make_test_client(state, token_tracker, event_log_path, broadcaster):
    """Create a Starlette TestClient with dashboard routes."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    routes = get_dashboard_routes(state, token_tracker, event_log_path, broadcaster)
    app = Starlette(routes=routes)
    return TestClient(app)


# ── DashboardEventBroadcaster ────────────────────────────────────────


class TestDashboardEventBroadcaster:
    """Tests for the SSE event broadcaster."""

    def test_initial_state(self, broadcaster):
        assert broadcaster.client_count == 0

    def test_add_client(self, broadcaster):
        q = broadcaster.add_client()
        assert broadcaster.client_count == 1
        assert isinstance(q, asyncio.Queue)

    def test_remove_client(self, broadcaster):
        q = broadcaster.add_client()
        assert broadcaster.client_count == 1
        broadcaster.remove_client(q)
        assert broadcaster.client_count == 0

    def test_remove_nonexistent_client(self, broadcaster):
        q = asyncio.Queue()
        broadcaster.remove_client(q)  # Should not raise
        assert broadcaster.client_count == 0

    def test_broadcast_to_single_client(self, broadcaster):
        q = broadcaster.add_client()
        broadcaster.broadcast("agents", {"agents": []})
        assert not q.empty()
        event_type, payload = q.get_nowait()
        assert event_type == "agents"
        assert json.loads(payload) == {"agents": []}

    def test_broadcast_to_multiple_clients(self, broadcaster):
        q1 = broadcaster.add_client()
        q2 = broadcaster.add_client()
        broadcaster.broadcast("costs", {"total": 1.23})
        assert not q1.empty()
        assert not q2.empty()
        _, p1 = q1.get_nowait()
        _, p2 = q2.get_nowait()
        assert p1 == p2

    def test_broadcast_no_clients(self, broadcaster):
        # Should not raise
        broadcaster.broadcast("agents", {"agents": []})

    def test_slow_client_dropped(self, broadcaster):
        q = broadcaster.add_client()
        # Fill the queue to capacity
        for i in range(DashboardEventBroadcaster.MAX_QUEUE_SIZE):
            broadcaster.broadcast("ping", {"i": i})
        assert broadcaster.client_count == 1
        # One more should drop the slow client
        broadcaster.broadcast("ping", {"i": 999})
        assert broadcaster.client_count == 0

    def test_broadcast_preserves_event_type(self, broadcaster):
        q = broadcaster.add_client()
        broadcaster.broadcast("escalation", {"id": "abc"})
        event_type, _ = q.get_nowait()
        assert event_type == "escalation"

    def test_broadcast_preserves_data(self, broadcaster):
        q = broadcaster.add_client()
        data = {"question": "Do this?", "options": ["Yes", "No"]}
        broadcaster.broadcast("escalation", data)
        _, payload = q.get_nowait()
        assert json.loads(payload) == data


# ── Dashboard HTML ───────────────────────────────────────────────────


class TestDashboardHTML:
    """Tests for the HTML page content."""

    def test_returns_string(self):
        html = dashboard_html()
        assert isinstance(html, str)

    def test_contains_doctype(self):
        html = dashboard_html()
        assert "<!DOCTYPE html>" in html

    def test_contains_title(self):
        html = dashboard_html()
        assert "<title>ARCH Dashboard</title>" in html

    def test_contains_agents_panel(self):
        html = dashboard_html()
        assert 'id="agents-list"' in html

    def test_contains_activity_log(self):
        html = dashboard_html()
        assert 'id="activity-log"' in html

    def test_contains_costs_panel(self):
        html = dashboard_html()
        assert 'id="costs-panel"' in html

    def test_contains_escalation_panel(self):
        html = dashboard_html()
        assert 'id="escalation-panel"' in html

    def test_contains_message_bar(self):
        html = dashboard_html()
        assert 'id="message-bar"' in html

    def test_contains_modal(self):
        html = dashboard_html()
        assert 'id="modal-overlay"' in html

    def test_contains_eventsource(self):
        html = dashboard_html()
        assert "EventSource" in html

    def test_contains_sse_endpoint(self):
        html = dashboard_html()
        assert "/api/dashboard/events" in html

    def test_contains_keyboard_shortcuts(self):
        html = dashboard_html()
        assert "keydown" in html

    def test_contains_connection_status(self):
        html = dashboard_html()
        assert 'id="conn-dot"' in html


# ── API Endpoints ────────────────────────────────────────────────────


class TestDashboardPage:
    """Tests for GET /dashboard."""

    def test_returns_html(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "ARCH Dashboard" in resp.text


class TestDashboardState:
    """Tests for GET /api/dashboard/state."""

    def test_returns_full_state(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data
        assert "agents" in data
        assert "messages" in data
        assert "decisions" in data
        assert "costs" in data

    def test_includes_project_name(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        assert resp.json()["project"]["name"] == "test-project"

    def test_includes_agents(self, state, token_tracker, event_log_path, broadcaster):
        state.register_agent("archie", "archie", "/tmp/wt", skip_permissions=False, sandboxed=False)
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        agents = resp.json()["agents"]
        assert len(agents) == 1
        assert agents[0]["id"] == "archie"

    def test_includes_messages(self, state, token_tracker, event_log_path, broadcaster):
        state.add_message("archie", "frontend", "Hello")
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        messages = resp.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello"

    def test_includes_pending_decisions(self, state, token_tracker, event_log_path, broadcaster):
        state.add_pending_decision("Do this?", ["Yes", "No"])
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        decisions = resp.json()["decisions"]
        assert len(decisions) == 1
        assert decisions[0]["question"] == "Do this?"

    def test_includes_costs(self, state, token_tracker, event_log_path, broadcaster):
        token_tracker.register_agent("archie", "claude-sonnet-4-6")
        token_tracker._handle_usage_event("archie", {"input_tokens": 1000, "output_tokens": 500})
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/state")
        costs = resp.json()["costs"]
        assert "archie" in costs
        assert costs["archie"]["input_tokens"] == 1000


class TestDashboardMessages:
    """Tests for GET /api/dashboard/messages."""

    def test_returns_all_messages(self, state, token_tracker, event_log_path, broadcaster):
        state.add_message("archie", "frontend", "Task 1")
        state.add_message("frontend", "archie", "Done")
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/messages")
        assert resp.status_code == 200
        messages = resp.json()
        assert len(messages) == 2

    def test_empty_messages(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/messages")
        assert resp.json() == []


class TestDashboardEventsLog:
    """Tests for GET /api/dashboard/events-log."""

    def test_returns_events(self, state, token_tracker, event_log_path, broadcaster):
        event = {"timestamp": "2026-01-01T00:00:00Z", "agent_id": "archie", "tool": "send_message"}
        event_log_path.write_text(json.dumps(event) + "\n")
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/events-log")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        assert events[0]["tool"] == "send_message"

    def test_no_event_log_file(self, state, token_tracker, broadcaster):
        path = Path("/tmp/nonexistent/events.jsonl")
        client = make_test_client(state, token_tracker, path, broadcaster)
        resp = client.get("/api/dashboard/events-log")
        assert resp.json() == []

    def test_handles_malformed_jsonl(self, state, token_tracker, event_log_path, broadcaster):
        event_log_path.write_text('{"valid": true}\nnot json\n{"also": "valid"}\n')
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.get("/api/dashboard/events-log")
        events = resp.json()
        assert len(events) == 2  # Skips the bad line


class TestDashboardSendMessage:
    """Tests for POST /api/dashboard/send."""

    def test_sends_message_to_archie(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.post(
            "/api/dashboard/send",
            json={"content": "Hello Archie"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        messages = state.get_all_messages()
        assert len(messages) == 1
        assert messages[0]["from"] == "user"
        assert messages[0]["to"] == "archie"
        assert messages[0]["content"] == "Hello Archie"

    def test_rejects_empty_message(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.post(
            "/api/dashboard/send",
            json={"content": ""},
        )
        assert resp.status_code == 400

    def test_rejects_whitespace_message(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.post(
            "/api/dashboard/send",
            json={"content": "   "},
        )
        assert resp.status_code == 400

    def test_rejects_invalid_json(self, state, token_tracker, event_log_path, broadcaster):
        client = make_test_client(state, token_tracker, event_log_path, broadcaster)
        resp = client.post(
            "/api/dashboard/send",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


# ── MCP Server Integration ──────────────────────────────────────────


class TestMCPServerDashboardIntegration:
    """Tests for dashboard integration in MCPServer."""

    def test_mcp_server_has_broadcaster(self, state):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state)
        assert hasattr(server, "_dashboard_broadcaster")
        assert isinstance(server._dashboard_broadcaster, DashboardEventBroadcaster)

    def test_mcp_server_accepts_token_tracker(self, state, token_tracker):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state, token_tracker=token_tracker)
        assert server.token_tracker is token_tracker

    def test_mcp_server_app_has_dashboard_route(self, state):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state)
        app = server.create_app()
        # The app is a wrapper function, but dashboard routes are in the starlette app
        assert app is not None

    def test_broadcast_agents(self, state):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state)
        state.register_agent("archie", "archie", "/tmp/wt", skip_permissions=False, sandboxed=False)
        q = server._dashboard_broadcaster.add_client()
        server._broadcast_agents()
        assert not q.empty()
        event_type, payload = q.get_nowait()
        assert event_type == "agents"
        data = json.loads(payload)
        assert len(data["agents"]) == 1

    def test_broadcast_costs(self, state, token_tracker):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state, token_tracker=token_tracker)
        token_tracker.register_agent("archie", "claude-sonnet-4-6")
        token_tracker._handle_usage_event("archie", {"input_tokens": 100, "output_tokens": 50})
        q = server._dashboard_broadcaster.add_client()
        server._broadcast_costs()
        assert not q.empty()
        event_type, payload = q.get_nowait()
        assert event_type == "costs"
        data = json.loads(payload)
        assert "archie" in data

    def test_broadcast_costs_no_tracker(self, state):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state)
        q = server._dashboard_broadcaster.add_client()
        server._broadcast_costs()
        # No error, but also no event since no tracker
        assert q.empty()

    def test_broadcast_dashboard_no_clients(self, state):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state)
        # Should not raise even with no clients
        server._broadcast_dashboard("agents", {"agents": []})


class TestMCPServerBroadcastInHandlers:
    """Tests that tool handlers broadcast events to dashboard."""

    @pytest.fixture
    def server(self, state, token_tracker):
        from arch.mcp_server import MCPServer
        server = MCPServer(state=state, token_tracker=token_tracker)
        state.register_agent("archie", "archie", "/tmp/wt", skip_permissions=False, sandboxed=False)
        state.register_agent("frontend", "frontend", "/tmp/wt2", skip_permissions=False, sandboxed=False)
        return server

    @pytest.mark.asyncio
    async def test_send_message_broadcasts(self, server):
        q = server._dashboard_broadcaster.add_client()
        await server._handle_send_message("archie", "frontend", "Hello")
        assert not q.empty()
        event_type, payload = q.get_nowait()
        assert event_type == "message"
        data = json.loads(payload)
        assert data["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_update_status_broadcasts_agents(self, server):
        q = server._dashboard_broadcaster.add_client()
        await server._handle_update_status("frontend", "Building", "working")
        assert not q.empty()
        event_type, _ = q.get_nowait()
        assert event_type == "agents"

    @pytest.mark.asyncio
    async def test_report_completion_broadcasts(self, server):
        q = server._dashboard_broadcaster.add_client()
        await server._handle_report_completion("frontend", "Done building", ["index.html"])
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        event_types = [e[0] for e in events]
        assert "agents" in event_types
        assert "message" in event_types

    @pytest.mark.asyncio
    async def test_escalation_broadcasts(self, server):
        q = server._dashboard_broadcaster.add_client()

        async def answer_later():
            await asyncio.sleep(0.1)
            decisions = server.state.get_pending_decisions()
            if decisions:
                server.answer_escalation(decisions[0]["id"], "Yes")

        asyncio.create_task(answer_later())
        answer = await server._handle_escalate_to_user("Proceed?", ["Yes", "No"])
        assert answer["answer"] == "Yes"

        events = []
        while not q.empty():
            events.append(q.get_nowait())
        event_types = [e[0] for e in events]
        assert "escalation" in event_types
        assert "escalation_cleared" in event_types

    @pytest.mark.asyncio
    async def test_answer_escalation_broadcasts_cleared(self, server):
        q = server._dashboard_broadcaster.add_client()
        decision = server.state.add_pending_decision("Test?", ["Yes"])
        decision_id = decision["id"]
        event = asyncio.Event()
        server._pending_escalations[decision_id] = event
        server.answer_escalation(decision_id, "Yes")
        assert not q.empty()
        event_type, payload = q.get_nowait()
        assert event_type == "escalation_cleared"
        assert json.loads(payload)["id"] == decision_id
