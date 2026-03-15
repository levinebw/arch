#!/usr/bin/env python3
"""
Launch a mock ARCH dashboard with sample data for screenshots.

Usage:
    python scripts/screenshot_dashboard.py

Opens http://localhost:3998/dashboard with pre-populated agent data,
messages, costs, and an active escalation. Take screenshots manually
then Ctrl+C to stop.
"""

import asyncio
import json
import sys
import tempfile
import webbrowser
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from arch.state import StateStore
from arch.token_tracker import TokenTracker
from arch.web_dashboard import DashboardEventBroadcaster, get_dashboard_routes

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


def populate_mock_data(state: StateStore, tracker: TokenTracker):
    """Add realistic mock data to state."""
    state.init_project("DataForge CLI", "A Python CLI tool for data transformations", "/tmp/dataforge")

    # Register agents
    state.register_agent("archie", "archie", "/tmp/wt/archie", skip_permissions=False, sandboxed=False)
    state.register_agent("backend-1", "backend", "/tmp/wt/backend", skip_permissions=False, sandboxed=False)
    state.register_agent("frontend-1", "frontend", "/tmp/wt/frontend", skip_permissions=False, sandboxed=False)
    state.register_agent("qa-1", "qa", "/tmp/wt/qa", skip_permissions=False, sandboxed=True)

    # Set agent statuses
    state.update_agent("archie", status="working", task="Coordinating agent team")
    state.update_agent("backend-1", status="working", task="Building core transformation library")
    state.update_agent("frontend-1", status="working", task="Implementing argparse CLI interface")
    state.update_agent("qa-1", status="idle", task="Waiting for implementation to complete")

    # Add messages
    state.add_message("archie", "backend-1", "Build the core data transformation library. Focus on CSV, JSON, and YAML formats. Use the stdlib only.")
    state.add_message("archie", "frontend-1", "Build the CLI interface using argparse. Commands: convert, validate, schema. Backend will expose a transform() function.")
    state.add_message("backend-1", "archie", "Starting work on the transformation library. I'll create dataforge/core.py with format detection and conversion functions.")
    state.add_message("frontend-1", "archie", "Starting CLI implementation. I'll set up the argparse structure with subcommands.")
    state.add_message("backend-1", "archie", "Core library complete: transform(), validate(), and detect_format() implemented with CSV/JSON/YAML support. 14 unit tests passing.")
    state.add_message("archie", "qa-1", "Backend and frontend are nearly done. Please prepare test plans for integration testing.")

    # Register agents in tracker and add usage
    tracker.register_agent("archie", "claude-opus-4-6")
    tracker.register_agent("backend-1", "claude-sonnet-4-6")
    tracker.register_agent("frontend-1", "claude-sonnet-4-6")
    tracker.register_agent("qa-1", "claude-sonnet-4-6")

    tracker._handle_usage_event("archie", {"input_tokens": 45000, "output_tokens": 8200})
    tracker._handle_usage_event("backend-1", {"input_tokens": 82000, "output_tokens": 15600})
    tracker._handle_usage_event("frontend-1", {"input_tokens": 61000, "output_tokens": 12300})
    tracker._handle_usage_event("qa-1", {"input_tokens": 5000, "output_tokens": 800})

    # Add a pending escalation
    state.add_pending_decision(
        "Backend reports the core library is complete with 14 tests passing. Frontend CLI is ready for integration. Should I proceed with connecting them and start QA testing?",
        ["Yes, proceed", "No, let me review first"]
    )


async def main():
    port = 3998
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        state = StateStore(state_dir=state_dir)
        tracker = TokenTracker(state_dir=state_dir)
        broadcaster = DashboardEventBroadcaster()
        event_log = state_dir / "events.jsonl"

        # Write some mock events
        events = [
            {"timestamp": "2026-03-15T12:09:20Z", "agent_id": "archie", "tool": "get_project_context", "args": {}, "result": {"status": "ok"}, "duration_ms": 45},
            {"timestamp": "2026-03-15T12:09:25Z", "agent_id": "archie", "tool": "list_personas", "args": {}, "result": {"status": "ok", "count": 6}, "duration_ms": 12},
            {"timestamp": "2026-03-15T12:09:30Z", "agent_id": "archie", "tool": "plan_team", "args": {"roles": ["backend", "frontend", "qa"]}, "result": {"status": "ok"}, "duration_ms": 890},
            {"timestamp": "2026-03-15T12:09:45Z", "agent_id": "archie", "tool": "spawn_agent", "args": {"agent_id": "backend-1", "role": "backend"}, "result": {"status": "ok", "agent_id": "backend-1"}, "duration_ms": 2300},
            {"timestamp": "2026-03-15T12:09:48Z", "agent_id": "archie", "tool": "spawn_agent", "args": {"agent_id": "frontend-1", "role": "frontend"}, "result": {"status": "ok", "agent_id": "frontend-1"}, "duration_ms": 2100},
            {"timestamp": "2026-03-15T12:10:00Z", "agent_id": "archie", "tool": "send_message", "args": {"to": "backend-1"}, "result": {"status": "ok"}, "duration_ms": 5},
            {"timestamp": "2026-03-15T12:10:02Z", "agent_id": "archie", "tool": "send_message", "args": {"to": "frontend-1"}, "result": {"status": "ok"}, "duration_ms": 4},
            {"timestamp": "2026-03-15T12:12:30Z", "agent_id": "backend-1", "tool": "update_status", "args": {"status": "working"}, "result": {"status": "ok"}, "duration_ms": 3},
            {"timestamp": "2026-03-15T12:15:00Z", "agent_id": "backend-1", "tool": "report_completion", "args": {"summary": "Core library complete"}, "result": {"status": "ok"}, "duration_ms": 8},
        ]
        with open(event_log, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        populate_mock_data(state, tracker)

        # Build routes
        async def handle_health(request):
            return JSONResponse({"status": "running", "port": port})

        # Handle escalation answers
        async def handle_escalation(request):
            decision_id = request.path_params.get("decision_id", "")
            body = await request.json()
            answer = body.get("answer", "")
            if state.answer_decision(decision_id, answer):
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": False}, status_code=404)

        dashboard_routes = get_dashboard_routes(state, tracker, event_log, broadcaster)
        routes = [
            Route("/api/health", handle_health),
            Route("/api/escalation/{decision_id}", handle_escalation, methods=["POST"]),
        ] + dashboard_routes

        app = Starlette(routes=routes)

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)

        print(f"\nDashboard running at: http://localhost:{port}/dashboard")
        print("Take screenshots, then press Ctrl+C to stop.\n")

        # Open browser after a short delay
        async def open_browser():
            await asyncio.sleep(1)
            webbrowser.open(f"http://localhost:{port}/dashboard")

        asyncio.create_task(open_browser())
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
