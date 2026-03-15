# ARCH Context Compaction & State Persistence — Design Document

## Current State

### What Works
- **`save_progress` MCP tool** — agents can persist structured state (progress, files_modified, next_steps, blockers, decisions) to `StateStore.agents[agent_id].context`
- **CLAUDE.md injection (Archie only)** — on Archie restart, saved context is injected as a `## Session State` section
- **`--resume` session restore** — Archie crash recovery passes `--resume {session_id}` to Claude CLI
- **Message cursor persistence** — `state/cursors.json` prevents re-reading old messages on restart

### What's Broken
1. **Worker agents don't get context injection** — `orchestrator.py` passes `session_state=` to `write_claude_md()` for Archie but not for workers. Saved progress is silently lost on worker restart.
2. **No compacting** — long-running agents that fill their context window have no recovery path.
3. **No context window monitoring** — no detection of approaching token limits.

---

## Research: How Others Handle It

### PAI (Personal AI Infrastructure)

PAI avoids compaction entirely. Instead it uses **selective loading with hard budgets**:

| Strategy | How |
|---|---|
| **Hot/Warm/Cold tiering** | Active state always loaded; recent learnings selectively loaded; old sessions on-demand only |
| **Budget-constrained injection** | Total context injection capped at < 2,000 characters across all categories |
| **Confidence-gated loading** | Only loads high-confidence learnings (>= 85% confidence score) |
| **On-demand context routing** | A routing map tells the agent which files to read for which task — load only what's needed |
| **Hook-driven lifecycle** | `LoadContext.hook.ts` fires at session start to inject context; `SessionCleanup.hook.ts` and `WorkCompletionLearning.hook.ts` fire at session end to harvest and persist |

Philosophy: **"Verifiable history over lossy compression."** Full transcripts stay on disk; only distilled summaries enter the context window.

**Key takeaway for ARCH:** Don't try to compress everything. Use structured summaries with character budgets. The save_progress tool already captures the right shape of data — it just needs to be reliably injected and budget-constrained.

### OpenClaw

OpenClaw implements aggressive, **reactive compaction** with a layered defense:

| Layer | Mechanism |
|---|---|
| **1. Bootstrap budget** | Initial context capped at 150K chars total, 20K per file, head-tail truncation (70% head / 20% tail) |
| **2. History turn limiting** | Only the last N user turns are retained (configurable per session type) |
| **3. Tool result truncation** | Per-result: 30% of context window. Per-message: 50%. Aggregate: 75%. Smart head+tail preservation for error messages |
| **4. Summarize-with-fallback** | Full summarization → staged summarization (split into chunks, summarize each, merge) → prune oldest chunks |
| **5. Overflow recovery** | On `context window exceeded` error: auto-compact up to 3x → truncate tool results → give up |
| **6. Orphan repair** | After pruning, `repairToolUseResultPairing()` removes orphaned `tool_result` entries to prevent API errors |

Key constants:
```
SAFETY_MARGIN = 1.2          (20% buffer on token estimates)
BASE_CHUNK_RATIO = 0.4       (40% of context window as initial chunk target)
MIN_CHUNK_RATIO = 0.15       (floor for adaptive chunking)
SUMMARIZATION_OVERHEAD = 4096 tokens
```

**Key takeaway for ARCH:** ARCH can't do in-process compaction like OpenClaw because agents are Claude CLI subprocesses — we don't control the context window directly. But we can detect overflow conditions and trigger a **save-and-restart cycle** that achieves the same effect.

---

## Design: ARCH Compaction Strategy

### Approach: Save-Summarize-Restart

Since ARCH agents are Claude CLI processes (not direct API calls), we can't manipulate their context window from outside. Instead:

1. **Monitor** token usage via stream-json events (we already parse these)
2. **Instruct** agents to call `save_progress` proactively via CLAUDE.md instructions
3. **Detect** when an agent is approaching its context limit
4. **Restart** the agent with a fresh context seeded by the saved progress summary

This is essentially PAI's philosophy (structured summaries, not lossy compression) combined with OpenClaw's monitoring approach (track tokens, react to limits).

### Implementation Plan

#### Fix 1: Worker Context Injection (Bug Fix)

**File:** `arch/orchestrator.py` — `_spawn_agent()` method

Before calling `write_claude_md()` for a worker, fetch any existing context:

```python
# Check for persisted session state from previous run
session_state = None
existing_agent = self.state.get_agent(agent_id)
if existing_agent and existing_agent.get("context"):
    session_state = existing_agent["context"]

self.worktree_manager.write_claude_md(
    agent_id=agent_id,
    # ... existing params ...
    session_state=session_state,
)
```

#### Fix 2: CLAUDE.md Save Instructions

Add instructions to agent CLAUDE.md telling them to call `save_progress` at key milestones. Inject into `worktree.py` `write_claude_md()`:

```markdown
## Context Persistence

Call the `save_progress` tool after completing each significant milestone:
- After finishing a file or feature
- Before starting a new phase of work
- When switching between tasks

This ensures your work survives restarts and context resets.
```

#### Fix 3: Token Budget Monitoring

**File:** `arch/token_tracker.py`

Add a method to check if an agent is approaching its context limit:

```python
def is_approaching_limit(self, agent_id: str, threshold: float = 0.80) -> bool:
    """Check if an agent's cumulative input tokens suggest context pressure."""
    agent = self._agents.get(agent_id)
    if not agent:
        return False
    # Claude's context window is ~200K tokens
    # Last API call's input_tokens reflects current context size
    return agent.last_input_tokens > (200_000 * threshold)
```

The stream-json `usage` events include `input_tokens` per API call. As the context fills, this number grows. When it crosses a threshold (e.g., 80% of 200K = 160K), we know the agent is running hot.

#### Fix 4: Compact-and-Restart Cycle

**File:** `arch/orchestrator.py`

When an agent's token usage crosses the threshold:

1. Send the agent a message via MCP: `"You are approaching your context limit. Call save_progress now with a summary of all work completed and next steps."`
2. Wait for `save_progress` to be called (or timeout after 60s)
3. Stop the agent session
4. Rewrite CLAUDE.md with the saved context injected as `## Session State`
5. Restart the agent with `--resume` disabled (fresh context, but with the summary)

```python
async def _compact_agent(self, agent_id: str) -> None:
    """Trigger a save-and-restart cycle for an agent approaching context limits."""
    # 1. Ask agent to save
    self.state.add_message(
        "system", agent_id,
        "CONTEXT LIMIT: Call save_progress immediately with full progress summary."
    )

    # 2. Wait for save_progress (poll state.get_agent context field)
    saved = await self._wait_for_save(agent_id, timeout=60)

    # 3. Stop agent
    await self._stop_agent_session(agent_id)

    # 4. Rewrite CLAUDE.md with saved context
    agent = self.state.get_agent(agent_id)
    self.worktree_manager.write_claude_md(
        agent_id=agent_id,
        # ... params ...
        session_state=agent.get("context"),
    )

    # 5. Restart fresh (no --resume, clean context)
    await self._restart_agent_fresh(agent_id)
```

#### Fix 5: Context Budget for Injected State

Apply PAI's lesson: cap the injected session state to prevent it from consuming too much of the fresh context window.

**File:** `arch/worktree.py` — `write_claude_md()`

```python
MAX_SESSION_STATE_CHARS = 2000

if session_state:
    state_text = self._format_session_state(session_state)
    if len(state_text) > MAX_SESSION_STATE_CHARS:
        # Truncate progress and next_steps, keep files_modified and decisions
        state_text = state_text[:MAX_SESSION_STATE_CHARS] + "\n(truncated)"
```

### Monitoring in Dashboard

Add a visual indicator in the Costs panel showing context pressure per agent. When an agent crosses 70% context usage, show a yellow indicator. At 85%, show red.

**File:** `arch/dashboard.py` — `CostsPanel._refresh_data()`

---

## What This Does NOT Do

- **No in-process context manipulation** — we can't reach into a running Claude CLI and remove messages. We restart instead.
- **No LLM-based summarization** — we don't make a separate API call to summarize the agent's work. The agent itself produces the summary via `save_progress`. This keeps costs zero.
- **No automatic learning extraction** — unlike PAI, we don't harvest learnings from transcripts. The `save_progress` tool is the single persistence mechanism.

## Priority Order

1. **Fix worker context injection** — bug fix, immediate
2. **Add save_progress instructions to CLAUDE.md** — low effort, high value
3. **Token budget monitoring** — requires parsing `input_tokens` from stream events
4. **Compact-and-restart cycle** — the actual compaction mechanism
5. **Dashboard indicators** — visibility into context pressure
6. **Context budget cap for injected state** — safety rail

---

## Open Questions

1. **Should Archie also compact?** Archie typically runs longer than workers. The same mechanism should apply.
2. **What's the right threshold?** 80% of 200K = 160K input tokens. Need to validate this against real usage data from UATs.
3. **How do we handle mid-tool-use compaction?** If the agent is in the middle of a multi-step operation when we send the compact signal, we risk losing work. The save_progress call should capture everything.
4. **Should we support `--resume` after compaction?** Using `--resume` restores the old context, defeating the purpose. Fresh start with injected summary is the right approach. But the agent loses its Claude session_id — need to track this as a "compacted" event, not a "crash."
