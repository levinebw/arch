# ARCH — Known Issues & Follow-up Tasks

Non-blocking issues found during code review. Address before v1 ship.
Each item notes which step introduced it and which step it must be fixed by.

---

## State Store (Step 1)

- [x] **No enum validation** — FIXED in Step 4. Added `validate_agent_status()` and `validate_task_status()` with `InvalidStatusError`.
- [ ] **No cascade deletion** — `remove_agent()` leaves orphaned tasks and messages referencing the removed agent. Document behavior or implement cleanup.
- [ ] **No JSON corruption recovery** — corrupted state files return None silently on load. Add try/except with logged warning.

---

## Worktree Manager (Step 2)

- [ ] **Missing subprocess timeouts** — all subprocess calls lack `timeout=` parameter. Git operations can hang indefinitely and freeze the harness. Add `timeout=30` minimum to all calls. Fix before Step 8 (Orchestrator).
- [ ] **Fragile PR number parsing** — PR number extracted by splitting URL string. Switch to `gh pr create --json number,url` and parse JSON output instead.
- [ ] **No PR creation tests** — `create_pr()` is the highest-risk method (depends on `gh` CLI, remote, GitHub auth) and has zero test coverage.
- [ ] **Silent branch deletion failure** — failed `git branch -d` after worktree removal is silently ignored. Add logging.
- [ ] **Type hint typo** — `dict[str, any]` should be `dict[str, Any]` (capital A from `typing`).
- [ ] **No logging** — no `logging` module integration. Add before Step 8 (Orchestrator) for debuggability.

---

## Token Tracker (Step 3)

- [ ] **Callback exception propagation** — if `on_usage_update` callback raises, it propagates through the token tracker and could crash stream parsing. Wrap in try/except. Fix in Step 9 (Dashboard) when wiring up callbacks.

---

## MCP Server (Step 4)

- [x] **POST /messages stubbed out** — FIXED. POST handler now routes messages through active transport.
- [x] **MCP server instance duplication** — FIXED. Added `get_or_create_mcp_server()` with instance caching in `_mcp_servers` dict.
- [ ] **BRIEF.md regex fails on whitespace** — regex for updating Current Status section assumes exact formatting. Whitespace variations cause silent failures.
- [ ] **GitHub CLI FileNotFoundError opaque** — when `gh` not installed, error message is generic `str(e)`. Add explicit handling with install instructions.
- [ ] **Logging inconsistent** — `logger` imported but only used in a few places. Add logging for all error paths.
- [x] **SSE handler TypeError on client disconnect** — FIXED. Wrapped Starlette app in ASGI middleware that catches TypeError from SSE disconnect.

---

## Session Manager (Step 5)

- [x] **Unread stderr can deadlock** — FIXED. Added `_process_stderr()` async task that reads stderr line-by-line and surfaces as system messages in state store.
- [ ] **Exit handling race** — `_process_output()` calls `_handle_exit()` after the read loop ends, but `stop()` can also cancel the output task and set `_running = False`. If both race, `_handle_exit` could fire twice. Add a guard at the top of `_handle_exit`.
- [ ] **Dead sessions accumulate** — `_wrap_exit_callback` in SessionManager doesn't remove finished sessions from `_sessions` dict. Stale entries grow over long runs. Add cleanup or periodic pruning.

---

## Container Manager (Step 6)

- [ ] **Unread stderr can deadlock (same as Step 5)** — `stderr=PIPE` is set but only exposed via optional `read_stderr()`. If the container writes heavily to stderr without anyone reading, pipe buffer fills and process deadlocks.
- [ ] **Timeout test uses wrong exception** — `test_check_docker_available_timeout` mocks `TimeoutError` but the code catches `subprocess.TimeoutExpired`. Test passes via the generic `except Exception` fallback, not the intended path.
- [ ] **No output parsing** — `ContainerSession` lacks the `_process_output` → `StreamParser` → `TokenTracker` pipeline that `Session` has. By design for Step 7 to integrate, but containerized agents won't track tokens until then.

---

## Container Integration (Step 7)

- [x] **No output parsing in ContainerSession** — FIXED. `ContainerizedSession` wraps `ContainerSession` with full `_process_output` → `StreamParser` → `TokenTracker` pipeline.

---

## Orchestrator (Step 8)

- [x] **CRITICAL: Agents block on permission approval** — FIXED ([#4](https://github.com/levinebw/arch/issues/4)). Implemented three-layer permission system:
  1. `--permission-mode acceptEdits` — auto-approves Read, Edit, Write, Glob, Grep
  2. `--allowedTools` whitelist — `DEFAULT_ALLOWED_TOOLS_ALL` (includes all worker MCP tools + git patterns) and `DEFAULT_ALLOWED_TOOLS_ARCHIE` (includes all Archie MCP tools + `Bash(gh *)`), merged with user-configured `permissions.allowed_tools` from arch.yaml
  3. `--permission-prompt-tool mcp__arch__handle_permission_request` — delegates unapproved tool requests to dashboard (enabled, not commented out)

  Runtime "always allow" also implemented:
  - Dashboard shows `[y]once [a]lways [n]o` for permission requests
  - "always" choice adds tool to in-memory `_runtime_allowed` dict (session-scoped)
  - Subsequent requests for same tool auto-approve without prompting

  Review fix (`db2f061`): Added MCP tools to default allowed lists (without them agents still blocked on first MCP call), fixed `Bash(git:*)` → `Bash(git *)` syntax, enabled `--permission-prompt-tool` (was commented out), moved `handle_permission_request` from `WORKER_TOOLS` to `SYSTEM_TOOLS` (callable by dispatch but not visible in agent tool catalogs).

  Files modified: orchestrator.py, session.py, container.py, mcp_server.py, dashboard.py
  Tests: 17 new tests (440 total)

- [ ] **`atexit` handler fires during tests** — "Emergency cleanup on exit" prints 8 times during test suite. The `atexit.register` in `_register_signal_handlers` is never unregistered. Add `atexit.unregister` in `_restore_signal_handlers` or guard the handler against test contexts.
- [ ] **`_permission_gate` uses blocking `input()`** — `input()` blocks the async event loop. Works for CLI usage but prevents automated/headless startup. Consider `asyncio.to_thread(input)` or a callback pattern.
- [x] **CRITICAL: No spawn_agent integration** — FIXED in Step 8 follow-up (commit `2aa2d9e`). Orchestrator now wires `on_spawn_agent`, `on_teardown_agent`, `on_request_merge`, `on_close_project` callbacks. 10 new lifecycle tests added.
- [ ] **`run()` loop polls at 1-second interval** — `await asyncio.sleep(1)` is a polling loop to check Archie's status. Consider using an `asyncio.Event` that gets set by the exit callback instead.
- [ ] **No token budget enforcement** — `token_budget_usd` is parsed from config and displayed in the cost summary, but never checked during runtime. Agents can exceed the budget without warning.
- [ ] **No BRIEF.md read at startup** — Spec says "Archie reads BRIEF.md at startup." The orchestrator creates the prompt telling Archie to read it, but doesn't inject its contents. If BRIEF.md is large, Archie may not read it immediately.
- [x] **CRITICAL: Relative `state_dir` breaks MCP config path** — FIXED. `state_dir: "./state"` in arch.yaml was used as-is (relative). `generate_mcp_config()` wrote config to `state/archie-mcp.json` and `--mcp-config state/archie-mcp.json` was passed to the claude subprocess. But the subprocess cwd is the agent's worktree (`.worktrees/archie/`), so the relative path didn't resolve. Claude never found the MCP config, never connected, 0 tokens. Fix: `Path(state_dir).resolve()` in orchestrator startup. Found in UAT #3.

---

## Dashboard (Step 9)

- [x] **`_refresh_task` never cancelled** — FIXED. Added `on_unmount` handler that cancels the refresh task.
- [x] **No exception handling in `_refresh_loop`** — FIXED. Wrapped in try/except to prevent silent crash.
- [ ] **`_seen_message_ids` grows unbounded** — Messages are tracked forever in the `_seen_message_ids` set to avoid duplicates, but there's no pruning. In long-running sessions with many messages, this could consume significant memory. Consider capping the set size or using message timestamps.
- [ ] **Escalation answer not verified** — After calling `answer_escalation()`, the UI immediately clears the escalation state without checking the return value. If `answer_escalation()` returns `False` (decision not found), the user loses their input with no error feedback. Check return value and show error if needed.
- [ ] **Timestamps display UTC, not local time** — `format_timestamp` shows UTC. For a local dev tool, local time is more natural. Consider `dt.astimezone().strftime(...)`.
- [ ] **No queued escalation count** — Only the first pending decision is shown (`decisions[0]`). If multiple escalations queue up, user has no indication of how many remain. Add a "(1 of N)" indicator.

---

## CLI (Step 12)

- [x] **CLI command conflicts with `/usr/bin/arch`** — FIXED (`dc002fb`). Renamed CLI entry point from `arch` to `archie`. Config file remains `arch.yaml` (system name, not CLI command). All help text, error messages, and examples updated.

---

## Agent Context & State Persistence

- [ ] **CRITICAL: Worker agents don't get context injection on restart** — When spawning worker agents, `orchestrator.py:1003-1011` does NOT pass `session_state` to `write_claude_md()`. Archie gets context injection (lines 804-827), but workers don't. If a worker calls `save_progress` and then crashes/restarts, the saved context sits in `agents.json` but is never re-injected into the agent's CLAUDE.md. Fix: fetch `state.get_agent(agent_id).context` before calling `write_claude_md()` and pass it as `session_state=`.
- [ ] **No context compacting strategy** — Long-running agents that hit Claude's context window have no automated recovery. No mechanism to detect approaching token limits, summarize completed work, or start fresh with just saved progress. The spec mentions "continuity across context compactions and restarts" (line 273-274) but nothing is implemented. Needs: (1) proactive `save_progress` calls before compacting, (2) compact-and-resume cycle, (3) context window monitoring.
- [ ] **No context window detection** — No mechanism to detect when an agent is approaching its token limit. Should monitor cumulative tokens and trigger a save-compact cycle before the agent hits the wall.

---

## Potential Enhancements (v2)

### ~~Detached Dashboard~~ — DONE

Implemented: `archie dashboard` runs as a standalone process, reads state from files, posts escalations via HTTP to MCP server's `/api/escalation/{decision_id}` endpoint. `archie up` runs orchestrator only.

### ~~Auto-Discovery of Agent Personas~~ — DONE

Implemented: `list_personas` MCP tool scans `personas/`, `agents/`, and system persona directories. `plan_team` tool lets Archie propose a team dynamically — `agent_pool` in arch.yaml is now optional. Team plans escalate to user for approval (or auto-approve with `auto_approve_team: true`).

### ~~MCP Event Logging~~ — DONE

Implemented: Every MCP tool call logged to `state/events.jsonl` with timestamp, agent_id, tool name, arguments, result summary, and duration. Dashboard `e` key opens event history viewer.

### ~~Dashboard Messaging~~ — DONE

Implemented: Dashboard input bar is always enabled. When no escalation is pending, typing sends a message from user to Archie. Auto-resume picks up unread messages and restarts Archie's session.

### Skills Integration — [#3](https://github.com/levinebw/arch/issues/3)

Add Claude Code Skills as a composable knowledge layer on top of personas. Personas define role identity ("who you are"); Skills define project conventions ("how we do things here"). Agents already inherit repo-level skills via worktrees — this enhancement adds role-specific skill mapping via `arch.yaml` and explicit injection at spawn time.
