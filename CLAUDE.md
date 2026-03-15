# ARCH — Project Guidelines

ARCH (Agent Runtime & Coordination Harness) is a multi-agent orchestration system. Implementation is **complete** (all 13 steps per SPEC-AGENT-HARNESS.md). The project is in maintenance and enhancement mode.

## Orientation

When starting a new session, read `HANDOFF.md` for full project state, recent changes, and architecture overview.

Key documents:
- `SPEC-AGENT-HARNESS.md` — full technical specification
- `HANDOFF.md` — current state, recent changes, architecture summary
- `KNOWN-ISSUES.md` — tracked bugs and follow-up tasks
- `COMPACTION-DESIGN.md` — designed but not yet implemented context compaction strategy
- `README.md` — user-facing documentation
- `docs/arch-architecture.excalidraw` — system architecture diagram

## Codebase

```
arch/
  state.py           — thread-safe state store (JSON persistence)
  worktree.py         — git worktree management, CLAUDE.md generation
  token_tracker.py    — stream-json parsing, per-agent cost tracking
  mcp_server.py       — SSE/HTTP MCP server, all agent tools
  session.py          — local Claude CLI subprocess management
  container.py        — Docker-based agent isolation
  orchestrator.py     — lifecycle management, spawning, auto-resume
  web_dashboard.py    — web dashboard (served from MCP server at /dashboard)
  dashboard.py        — Textual TUI (deprecated, retained for tests)

arch.py               — CLI entrypoint (archie up/down/dashboard/status/init/send)
personas/*.md         — agent role definitions (archie, frontend, backend, qa, security, copywriter)

tests/
  test_state.py, test_worktree.py, test_token_tracker.py, test_mcp_server.py,
  test_session.py, test_container.py, test_orchestrator.py, test_dashboard.py,
  test_dashboard_e2e.py, test_web_dashboard.py, test_cli.py,
  test_integration.py, test_e2e.py, test_smoke.py, test_uat.py
```

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

557 tests. 3 pre-existing failures (2 Docker-not-running, 1 UAT) are expected.

For a single test file: `python -m pytest tests/test_mcp_server.py -v`

## Working on This Project

- **Bug fixes**: Check `KNOWN-ISSUES.md` for tracked issues with context.
- **Enhancements**: The compaction system in `COMPACTION-DESIGN.md` is designed but not implemented.
- **UATs**: `tests/uat5.sh` through `tests/uat8.sh` are acceptance test scripts. UATs 5-7 have passed.
- **Tests**: Write tests for any new code. Run the full suite before committing.
- **Spec reference**: When in doubt about intended behavior, check `SPEC-AGENT-HARNESS.md`.

## Constraints

- Python 3.11+
- GitHub repo: https://github.com/levinebw/arch

## User Preferences

- **No self-attribution**: Do NOT add "Co-Authored-By: Claude" or similar attribution lines to commits, PRs, documents, or any other content unless explicitly instructed by the user.

## Compacting and Resuming

- When compacting, update your memory and update `HANDOFF.md`.
- When resuming, read `HANDOFF.md` for full context.
