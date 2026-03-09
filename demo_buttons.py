"""Visual demo of dashboard escalation buttons."""
import asyncio
import tempfile
from pathlib import Path

from arch.dashboard import Dashboard
from arch.state import StateStore
from arch.token_tracker import TokenTracker


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        state = StateStore(state_dir)
        tracker = TokenTracker(state_dir=state_dir)

        # Set up project
        state._state["project"] = {"name": "Button Demo", "status": "active"}

        # Register some agents
        state.register_agent("archie", "lead", "/tmp/archie")
        state.update_agent("archie", status="working", task="Planning team")
        state.register_agent("frontend-1", "frontend", "/tmp/frontend")
        state.update_agent("frontend-1", status="working", task="Building UI")

        # Add some activity messages
        state.add_message("archie", "frontend-1", "Build the landing page with hero, features, and pricing sections")
        state.add_message("frontend-1", "archie", "Got it, starting on the hero section now")

        # Add a pending decision WITH options (this is what Archie sends via plan_team)
        state.add_pending_decision(
            "Proposed team for CloudSync Landing Page:\n\n"
            "1. frontend (personas/frontend.md) - Build landing page\n"
            "2. qa (personas/qa.md) - Write validation tests\n\n"
            "Rationale: Project needs frontend + QA. No backend needed.",
            ["Approve team", "Reject team", "Modify team"]
        )

        app = Dashboard(state=state, token_tracker=tracker)
        await app.run_async()


if __name__ == "__main__":
    asyncio.run(main())
