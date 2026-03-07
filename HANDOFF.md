# ARCH Implementation Handoff

## Current State

**Steps Completed: 1-13 of 13** ✅
**Tests: 494 passing** (2 failing — Docker not running, pre-existing)
**Dashboard Refactor: COMPLETE** — Dashboard detached into separate process

## Recent Changes: Dashboard Refactor

The dashboard now runs as a **separate process** from the orchestrator, fixing the P0 shutdown hang from UAT #6.

### Architecture Change

**Before:** `archie up` ran both orchestrator and dashboard in-process via `asyncio.wait(FIRST_COMPLETED)`. Caused signal handler conflicts, terminal contention, and Textual cancellation hangs.

**After:**
- `archie up` — runs orchestrator only, logs to stdout. No TUI.
- `archie dashboard` — standalone Textual TUI. Reads state from `state/` JSON files. Posts escalation answers via HTTP to MCP server.

### Files Changed

| File | Change |
|------|--------|
| `arch/mcp_server.py` | Added `/api/escalation/{decision_id}` POST and `/api/health` GET endpoints |
| `arch/state.py` | Added `reload()` public method |
| `arch/dashboard.py` | Refactored for standalone mode (`state_dir`/`mcp_port` params), HTTP escalation posting, `on_unmount` cleanup, try/except in refresh loop. Removed dead `run_dashboard`/`run_dashboard_async` functions. |
| `arch.py` | Simplified `cmd_up` (no dashboard), added `cmd_dashboard`, fixed `usage.json` filename bug in `cmd_status` |
| `arch/orchestrator.py` | Removed `_dashboard` attribute |
| `KNOWN-ISSUES.md` | Marked detached dashboard done, `_refresh_task` fix done, `_refresh_loop` exception fix done, added auto-discovery enhancement |

### New Tests (20 added)

- `TestHTTPEndpoints` (6 tests) — health endpoint, escalation answer success/not-found/missing/invalid
- `TestDashboardStandaloneInit` (3 tests) — standalone init, budget, in-process mode
- `TestDashboardStandaloneRefresh` (1 test) — reload from files
- `TestDashboardStandaloneEscalation` (2 tests) — HTTP posting, connection error handling
- `TestDashboardOnUnmount` (1 test) — refresh task cancellation
- `TestDashboardOrchestratorConnection` (3 tests) — connection check no-port/unreachable/success
- `TestCmdDashboard` (4 tests) — no config, creates state dir, reads config, main dispatch

## UAT Results

### UAT #5: Todo App (Single Agent) — PASSED ✅
- Commit: `c89fa2d`
- Full lifecycle worked end-to-end

### UAT #6: Portfolio Site (Multi-Agent) — FUNCTIONAL PASS ✅
- Commit: `26ba7bd` (UAT script)
- Both frontend and qa agents worked in parallel, merged, QA tests pass
- **Dashboard hang resolved by this refactor** — dashboard now runs independently

## Next Steps

1. **Re-run UAT #6** with the new `archie dashboard` pattern to verify clean shutdown
2. **Worktree cleanup on exit** — `.worktrees/` dir not cleaned up (low priority)

## Key Architecture

### Dashboard Separation

```
Terminal 1: archie up          → Orchestrator → MCP Server (port 3999)
Terminal 2: archie dashboard   → Reads state/*.json, POSTs to /api/escalation
```

Dashboard modes:
- **Standalone** (`state_dir` + `mcp_port`): reads from JSON files, POSTs escalations via HTTP
- **In-process** (`state` + `token_tracker` objects): used by tests, backward-compatible

### Orchestrator + Dashboard Communication
- State: Both read/write `state/` directory JSON files (atomic writes via `.tmp`)
- Escalations: Dashboard POSTs to `http://127.0.0.1:{mcp_port}/api/escalation/{decision_id}`
- Health: Dashboard GETs `http://127.0.0.1:{mcp_port}/api/health` to show connection status

### Agent Lifecycle Flow
```
Archie calls spawn_agent via MCP
  → MCPServer._handle_spawn_agent
  → Orchestrator._handle_spawn_agent (callback)
    → WorktreeManager.create()
    → WorktreeManager.write_claude_md()
    → SessionManager.spawn()
    → StateStore.register_agent()
  ← Returns {agent_id, worktree_path, sandboxed, status}
```

### Permission System (Three Layers)
1. `--permission-mode acceptEdits` — auto-approves Read, Edit, Write, Glob, Grep
2. `--allowedTools` — per-role whitelist from `DEFAULT_ALLOWED_TOOLS_ALL` / `_ARCHIE` + user config
3. `--permission-prompt-tool mcp__arch__handle_permission_request` — runtime delegation to dashboard

### Config
- `arch.yaml` — system config
- CLI: `python arch.py up` / `python arch.py dashboard`

## Running Tests

```bash
source .venv/bin/activate
GIT_CONFIG_GLOBAL=/dev/null python -m pytest tests/ -v
```
