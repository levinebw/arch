# ARCH Implementation Handoff

## Current State

**Steps Completed: 1-13 of 13** ✅
**Tests: 440 passing**
**Last Commit:** dc002fb

## Completed Components

| Step | File | Description | Tests |
|------|------|-------------|-------|
| 1 | `arch/state.py` | Thread-safe state store, JSON persistence, enum validation | 55 |
| 2 | `arch/worktree.py` | Git worktree create/remove/merge, CLAUDE.md injection | 28 |
| 3 | `arch/token_tracker.py` | Stream-json parsing, cost calculation, pricing.yaml | 32 |
| 4 | `arch/mcp_server.py` | SSE/HTTP MCP server, access controls, all tools, stop() | 40 |
| 5 | `arch/session.py` | Local claude subprocess, output parsing, resume | 34 |
| 6 | `arch/container.py` | Docker spawn/stop, volume mounts, Dockerfile | 40 |
| 7 | `arch/session.py` | Unified Session/Container interface, ContainerizedSession | 16 |
| 8 | `arch/orchestrator.py` | Config parsing, gates, startup/shutdown, lifecycle wiring | 53 |
| 9 | `arch/dashboard.py` | Textual TUI with agents/activity/costs panels, escalations | 43 |
| 10 | `personas/*.md` | archie, frontend, backend, qa, security, copywriter personas | - |
| 11 | `tests/test_mcp_server.py` | GitHub tools integration tests (mocked gh CLI) | 22 |
| 11.5 | `arch/*.py` | Agent state persistence: context field, save_progress tool, CLAUDE.md injection | 16 |
| 12 | `arch.py` | CLI entrypoint: up/down/status/init/send commands, PID file, GitHub label setup | 31 |
| 13 | `tests/test_integration.py` | End-to-end integration tests with real git operations | 16 |

## All Steps Complete

ARCH v1 implementation is complete per SPEC-AGENT-HARNESS.md.

## Post-Build Fixes

### Issue #4: Agent permissions — FIXED

UAT revealed agents blocked on permission prompts (no TTY). Builder implemented three-layer permission system (`e633bf9`). Review found 4 bugs; fixed in `db2f061`:

1. MCP tools missing from default allowed lists (agents still blocked on every MCP call)
2. Wrong Bash pattern syntax: `Bash(git:*)` → `Bash(git *)` per Claude CLI docs
3. `--permission-prompt-tool` was commented out (no race condition — MCP starts before agents)
4. `handle_permission_request` was in `WORKER_TOOLS` (agents could call it directly) — moved to `SYSTEM_TOOLS`

### CLI rename: `arch` → `archie` — FIXED

`/usr/bin/arch` is a macOS system binary. Renamed CLI entry point to `archie` (`dc002fb`). Config file stays `arch.yaml`.

---

## Key Architecture

### Dashboard Features (Step 9)

**Layout:**
```
┌─────────────────────────────────────────────────────────────────────┐
│  ARCH  ·  ProjectName  ·  Runtime: 00:14:32      [q]uit  [?]help   │
├───────────────┬──────────────────────────────────┬──────────────────┤
│ AGENTS        │ ACTIVITY LOG                     │ COSTS            │
│               │                                  │                  │
│ ● archie      │ 14:01 archie   Spawning fe-dev-1 │ archie   $0.12   │
│   Coordinating│ 14:02 fe-dev   Starting NavBar   │ fe-dev   $0.04   │
│               │ 14:03 qa-1     Running tests     │ qa-1     $0.02   │
│ ●[c] fe-dev-1 │ 14:04 fe-dev   BLOCKED: needs API│ ──────────────   │
│   Building    │ 14:05 archie   Checking in       │ Total    $0.19   │
│   NavBar      │                                  │ Budget   $5.00   │
│               │                                  │ ████░░   3.8%    │
│ ●[c][!] sec-1 │                                  │                  │
├───────────────┴──────────────────────────────────┴──────────────────┤
│ ⚠ ARCHIE ASKS: Merge frontend-dev-1 worktree to main? [y/N]: _     │
└─────────────────────────────────────────────────────────────────────┘
```

**Status indicators:**
- `●` green — working
- `●` yellow — blocked/waiting_review
- `○` bright_black — idle
- `✓` green — done
- `✗` red — error
- `[c]` — containerized
- `[!]` — skip_permissions

**Keyboard shortcuts:**
- `q` — graceful shutdown
- `?` — help overlay
- `l` — Archie's log
- `1-9` — agent logs
- `m` — message bus

**Integration:**
- `StateStore.list_agents()` → agents panel
- `StateStore.get_all_messages()` → activity log
- `StateStore.get_pending_decisions()` → escalation panel
- `TokenTracker.get_all_usage()` → costs panel
- `MCPServer.answer_escalation()` → handles user input
- 2-second refresh interval

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

### Session Types
- `Session` — local subprocess
- `ContainerizedSession` — Docker container with stream parsing
- `AnySession = Session | ContainerizedSession`
- `SessionManager.spawn()` auto-delegates based on `config.sandboxed`

### Config file
- `arch.yaml` (renamed from archie.yaml — system config, not persona)

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Quick Verification

```bash
source .venv/bin/activate
python -c "
from arch.orchestrator import Orchestrator
from arch.mcp_server import MCPServer
from arch.session import SessionManager, ContainerizedSession
from arch.state import StateStore
from arch.dashboard import Dashboard, run_dashboard
print('All modules ready')
"
```
