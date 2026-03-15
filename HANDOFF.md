# ARCH Implementation Handoff

## Current State

**Steps Completed: 1-13 of 13** ✅
**Tests: 557 collected** (3 pre-existing failures: 2 Docker, 1 UAT)
**Post-v1 enhancements: shipped** — see Recent Changes below

## Recent Changes (2026-03-15)

### Web Dashboard (replaces Textual TUI)
- New `arch/web_dashboard.py` — single-page HTML/CSS/JS dashboard served from MCP server at `/dashboard`
- `DashboardEventBroadcaster` pushes real-time updates via SSE to browser clients
- New API endpoints: `/api/dashboard/state`, `/api/dashboard/events`, `/api/dashboard/messages`, `/api/dashboard/events-log`, `/api/dashboard/send`
- MCP server tool handlers broadcast state changes (agents, messages, escalations, costs, events)
- `archie dashboard` now opens browser instead of Textual TUI
- Textual dashboard (`arch/dashboard.py`) deprecated but retained for backward compatibility
- 51 new tests in `tests/test_web_dashboard.py`
- Screenshots: `docs/dashboard-web.png`, `docs/dashboard-costs.png`, `docs/dashboard-escalation.png`, `docs/dashboard-messages-web.png`, `docs/dashboard-events.png`

### Expanded Agent Permissions
- Added `yarn`, `pnpm`, `bun`, `deno`, `cargo`, `go`, `make`, `docker`, `curl`, `wget`, `kill`, `lsof`, and many more to `DEFAULT_ALLOWED_TOOLS_ALL`
- Agents can now launch dev servers, run build tools, test APIs, and manage background processes

## Earlier Changes (2026-03-14)

### Documentation & Diagrams
- `README.md` updated with dashboard screenshots (main view, help, messages, events)
- `docs/arch-architecture.excalidraw` — full system architecture diagram
- `COMPACTION-DESIGN.md` — context compaction strategy designed (not yet implemented)
- `KNOWN-ISSUES.md` — updated with agent context persistence issues and completed v2 enhancements

## Earlier Changes (2026-03-13)

### Agent Output Logging (51163e1)
- `on_output` callback wired in orchestrator — logs agent text (truncated 200 chars) and tool calls
- `archie up` terminal now shows what agents are thinking and doing

### Escalation Buttons (a656dd2)
- Dashboard escalation panel renders clickable `Button` widgets when options provided
- Permission requests show "Yes (once)" / "Always" / "No" buttons
- Free-text input remains below buttons for custom answers
- **Root cause fix:** `options` was set AFTER `question` in `_refresh_data()`, so `watch_question` ran before `self.options` was populated. Fixed by setting `options` before `question`.

### Worker Context Injection (a656dd2)
- Worker agents now get saved context (`save_progress`) injected into CLAUDE.md on restart
- Previously only Archie had this — workers lost context on crash/restart

### close_project User Confirmation (568392f)
- `close_project` escalates to user before shutting down: "Is everything done?"
- Only explicit "Yes" confirms shutdown
- Any other response (including custom feedback) forwarded to Archie as actionable instructions
- BRIEF.md only updated to COMPLETE after user confirms (not before)
- **Bug fixed:** Operator precedence caused custom feedback to fall through to shutdown

### update_brief done_when Section (7252816)
- `update_brief(section: "done_when", content: "substring")` checks off matching checklist items
- Archie persona updated with explicit instructions to check off items after each merge

### Dev Tool Permissions (5c9808c)
- Agents can now run `python`, `node`, `npm`, `npx`, `pip`, `cat`, `ls`, `find`, etc.
- Previously only `Bash(git *)` was allowed — agents escalated test execution to user

### plan_team Permission Fix (d0301fc)
- `mcp__arch__list_personas` and `mcp__arch__plan_team` were missing from `DEFAULT_ALLOWED_TOOLS_ARCHIE`
- Archie's calls hit the permission system, which has a different schema (`reason` required), causing validation errors

### Dashboard UX (96ae1db)
- Costs panel hidden by default — toggle with `c` key
- Activity log messages no longer truncated (was 47 chars!)
- Default Archie model updated to `claude-opus-4-6`

### Auto-merge Hardcoded Branch
- Auto-merge on teardown hardcodes `"main"` — should use current branch
- Latent bug, not yet fixed (doesn't affect most runs)

## UAT Results

### UAT #5: Todo App (Single Agent) — PASSED ✅
### UAT #6: Portfolio Site (Multi-Agent) — FUNCTIONAL PASS ✅
### UAT #7: CloudSync Landing Page (Dynamic Team Planning) — FUNCTIONAL PASS ✅
- Team planning worked: Archie proposed frontend + qa (skipped backend) ✓
- Escalation buttons rendered correctly ✓
- Agents built landing page and tests ✓
- Discovered bugs: plan_team permission error (fixed), close_project custom feedback (fixed), BRIEF.md not updated (fixed), agents couldn't run python tests (fixed)
- UAT #8 script written but not yet run

## Key Architecture

```
Terminal 1: archie up          → Orchestrator → MCP Server (port 3999)
Browser:   localhost:3999/dashboard  → Web dashboard (SSE real-time updates)
```

### Agent Lifecycle Flow
```
Archie calls plan_team via MCP
  → User approves team in dashboard
  → Roles added to runtime agent_pool
Archie calls spawn_agent for each role
  → WorktreeManager.create()
  → SessionManager.spawn()
  → StateStore.register_agent()
Agent works, calls report_completion
Archie calls request_merge (or teardown auto-merges)
Archie calls close_project → user confirms → BRIEF.md updated → shutdown
```

### MCP Tools
- **All agents:** send_message, get_messages, update_status, report_completion, save_progress
- **Archie only:** spawn_agent, teardown_agent, list_agents, escalate_to_user, request_merge, get_project_context, close_project, update_brief, list_personas, plan_team
- **GitHub (Archie):** gh_create_issue, gh_list_issues, gh_close_issue, gh_update_issue, gh_add_comment, gh_create_milestone, gh_list_milestones

### State Files
- `state/agents.json` — agent registry
- `state/messages.json` — message bus
- `state/pending_decisions.json` — escalations
- `state/usage.json` — per-agent token counts and costs
- `state/events.jsonl` — MCP tool call history (append-only)

## Running Tests

```bash
source .venv/bin/activate
GIT_CONFIG_GLOBAL=/dev/null python -m pytest tests/ -v
```

## Outstanding Work

### Not Yet Implemented
- **Context compaction** — full design in `COMPACTION-DESIGN.md`, 6 fixes outlined (monitoring, compact-and-restart cycle, dashboard indicators)
- **Token budget enforcement** — `token_budget_usd` is parsed and displayed but never enforced at runtime
- **UAT #8** — script written (`tests/uat8.sh`) but not yet run

### Known Bugs (see KNOWN-ISSUES.md for full list)
- `atexit` handler prints "Emergency cleanup" 8 times during test suite
- `_permission_gate` uses blocking `input()` on async event loop
- `run()` loop polls with `asyncio.sleep(1)` instead of using an Event
- Auto-merge on teardown hardcodes `"main"` branch
- `_seen_message_ids` in dashboard grows unbounded
