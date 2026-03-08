# ARCH Implementation Handoff

## Current State

**Steps Completed: 1-13 of 13** ✅
**Tests: 495 passing** (2 failing — Docker not running, pre-existing)
**Post-v1 enhancements: shipped** — see Recent Changes below

## Recent Changes (2026-03-08)

### Token Tracking Fix
- Stream parser was looking for `"type": "usage"` events that don't exist in claude CLI output
- Real usage data comes in `"type": "assistant"` events at `message.usage`
- All test fixtures updated to match real stream-json format

### MCP Event Log
- Every tool call appended to `state/events.jsonl` with timestamp, agent_id, tool, args, result, duration_ms
- Dashboard `e` key opens event history viewer (`EventLogScreen`)

### Dynamic Team Planning
- `list_personas` tool — scans project `personas/`, `agents/`, and system `personas/` dirs
- `plan_team` tool — Archie proposes team, orchestrator escalates to user for approval
- On approval, roles added to runtime `agent_pool`
- `agent_pool` in arch.yaml is now optional (empty = Archie decides)
- `auto_approve_team` setting in arch.yaml skips approval step
- Archie persona updated with team planning instructions

### Dashboard Enhancements
- Input bar always enabled — sends messages to Archie when no escalation pending
- Message filters fixed: agent views show messages FROM that agent only
- Refactored `on_input_submitted` into `_submit_escalation_answer` + `_submit_message_to_archie`
- Escalation internals refactored: `_escalate_and_wait` reusable method

### Auto-merge on Teardown
- `teardown_agent` checks for unmerged commits and auto-merges before removing worktree
- Fixes lost work when Archie calls teardown without explicit `request_merge`

### BRIEF.md Auto-update
- `close_project` auto-updates BRIEF.md current status with summary
- Sends system message to Archie to review and finalize the brief

## UAT Results

### UAT #5: Todo App (Single Agent) — PASSED ✅
### UAT #6: Portfolio Site (Multi-Agent) — FUNCTIONAL PASS ✅
- Both frontend and qa agents worked in parallel, both merged, QA tests pass
- Known issue from earlier run: qa-2's `test_site.py` was lost (fixed by auto-merge on teardown)

## Key Architecture

```
Terminal 1: archie up          → Orchestrator → MCP Server (port 3999)
Terminal 2: archie dashboard   → Reads state/*.json, POSTs to /api/escalation
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
Archie calls close_project → BRIEF.md updated → shutdown
```

### MCP Tools
- **All agents:** send_message, get_messages, update_status, report_completion, save_progress
- **Archie only:** spawn_agent, teardown_agent, list_agents, escalate_to_user, request_merge, get_project_context, close_project, update_brief, list_personas, plan_team
- **GitHub (Archie):** gh_create_issue, gh_list_issues, gh_close_issue, gh_update_issue, gh_add_comment, gh_create_milestone, gh_list_milestones

### State Files
- `state/state.json` — agents, messages, decisions, project status
- `state/usage.json` — per-agent token counts and costs
- `state/events.jsonl` — MCP tool call history (append-only)

## Running Tests

```bash
source .venv/bin/activate
GIT_CONFIG_GLOBAL=/dev/null python -m pytest tests/ -v
```
