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

---

## Session Manager (Step 5)

- [ ] **Unread stderr can deadlock** — `stderr=asyncio.subprocess.PIPE` is set but stderr is never consumed. If the subprocess writes enough to stderr, the pipe buffer fills and the process deadlocks. Either read stderr concurrently or use `asyncio.subprocess.DEVNULL`.
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

- [x] **CRITICAL: Agents block on permission approval** — FIXED ([#4](https://github.com/AppSecHQ/arch/issues/4)). Implemented three-layer permission system:
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

---

## Dashboard (Step 9)

- [ ] **`_refresh_task` never cancelled** — The asyncio task created in `on_mount` runs an infinite `while True` loop and is never cancelled when the app exits. This can cause "Task was destroyed but it is pending" warnings. Add task cancellation in an `on_unmount` handler or use Textual's `set_interval` instead.
- [ ] **No exception handling in `_refresh_loop`** — If `_refresh_data()` raises any exception (e.g., state store unavailable, widget query fails), the entire refresh loop crashes silently and the dashboard stops updating with no indication to the user. Wrap in try/except with logging.
- [ ] **`_seen_message_ids` grows unbounded** — Messages are tracked forever in the `_seen_message_ids` set to avoid duplicates, but there's no pruning. In long-running sessions with many messages, this could consume significant memory. Consider capping the set size or using message timestamps.
- [ ] **Escalation answer not verified** — After calling `answer_escalation()`, the UI immediately clears the escalation state without checking the return value. If `answer_escalation()` returns `False` (decision not found), the user loses their input with no error feedback. Check return value and show error if needed.
- [ ] **Timestamps display UTC, not local time** — `format_timestamp` shows UTC. For a local dev tool, local time is more natural. Consider `dt.astimezone().strftime(...)`.
- [ ] **No queued escalation count** — Only the first pending decision is shown (`decisions[0]`). If multiple escalations queue up, user has no indication of how many remain. Add a "(1 of N)" indicator.

---

## CLI (Step 12)

- [x] **CLI command conflicts with `/usr/bin/arch`** — FIXED (`dc002fb`). Renamed CLI entry point from `arch` to `archie`. Config file remains `arch.yaml` (system name, not CLI command). All help text, error messages, and examples updated.

---

## Potential Enhancements (v2)

### Skills Integration — [#3](https://github.com/AppSecHQ/arch/issues/3)

Add Claude Code Skills as a composable knowledge layer on top of personas. Personas define role identity ("who you are"); Skills define project conventions ("how we do things here"). Agents already inherit repo-level skills via worktrees — this enhancement adds role-specific skill mapping via `arch.yaml` and explicit injection at spawn time.
