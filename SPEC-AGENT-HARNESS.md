# ARCH — Agent Runtime & Coordination Harness
## Technical Specification

---

## Overview

**ARCH** is a generalized multi-agent development system that orchestrates multiple independent Claude CLI sessions working concurrently on a software project. Each agent is a full `claude` CLI process with its own persona, git worktree, and persistent memory. A central harness process connects all agents via a local MCP server, tracks token usage, and renders a live dashboard.

**Archie** is the Lead Agent — the friendly, intelligent coordinator that interfaces with the user, decomposes work, and manages the team. Archie is the face of ARCH.

```
$ arch up
    _   ____   ____  _   _
   / \ |  _ \ / ___|| | | |
  / _ \| |_) | |    | |_| |
 / ___ \  _ <| |___ |  _  |
/_/   \_\_| \_\\____||_| |_|

Agent Runtime & Coordination Harness v1.0
Hi, I'm Archie. Let's build something great.

✓ Reading arch.yaml...
✓ Initializing git worktrees...
✓ Starting MCP server on :3999...
✓ Archie is online.
```

**Primary language:** Python 3.11+
**Key dependencies:** `anthropic`, `mcp`, `textual`, `gitpython`, `pyyaml`, `asyncio`, `docker`

---

## Repository Structure

```
arch/
├── arch.py                     # Main entrypoint (CLI: arch up / down / status / init)
├── arch.yaml                 # User-facing project config (see schema below)
├── BRIEF.md                    # Persistent project brief — goals, constraints, status, decisions
├── requirements.txt
│
├── arch/
│   ├── __init__.py
│   ├── orchestrator.py         # Lifecycle: reads config, spawns agents, teardown
│   ├── mcp_server.py           # MCP server over SSE/HTTP (message bus + state + tools)
│   ├── session.py              # Manages individual claude CLI subprocesses (local or container)
│   ├── container.py            # Docker container lifecycle management
│   ├── worktree.py             # Git worktree creation and cleanup
│   ├── token_tracker.py        # Parses claude stream-json output, accumulates usage per agent
│   ├── state.py                # Shared in-memory + persisted state store
│   └── dashboard.py            # Textual TUI dashboard
│
├── personas/
│   ├── archie.md               # Archie (Lead Agent) persona — CLAUDE.md template
│   ├── frontend.md
│   ├── backend.md
│   ├── qa.md
│   ├── security.md
│   └── copywriter.md
│
└── state/                      # Runtime state (gitignored in target project)
    ├── agents.json             # Live agent registry
    ├── messages.json           # Message bus log
    ├── usage.json              # Token usage per agent
    ├── tasks.json              # Task assignments
    ├── archie-cursor.json      # Persisted message read cursor (since_id) — survives compaction
    └── permissions_audit.log   # Timestamped log of --dangerously-skip-permissions usage
```

---

## BRIEF.md — Project Brief

`BRIEF.md` lives in the project root alongside `arch.yaml`. It is the persistent source of truth for project goals, constraints, and current state across sessions. Archie reads it at startup and updates it at shutdown.

**The file is human-editable** — the user can update it at any time between sessions to redirect Archie.

```markdown
# BRIEF.md

## Goal
What we are building and why. Written by the user; refined by Archie.

## Done When
Specific, verifiable success criteria. What does "finished" look like?
Use concrete, checkable statements — not vague descriptors.

## Constraints
- Known technical decisions already locked in
- Things to avoid or not change
- External dependencies or deadlines

## Current Status
_Updated by Archie at the end of each session._
Where the project stands right now. What is working. What is in progress.

## Decisions Log
_Appended by Archie when significant decisions are made._
| Date | Decision | Rationale |
|------|----------|-----------|
```

### How Archie uses BRIEF.md

**At startup:** `get_project_context` reads `BRIEF.md` and includes its full contents in Archie's context. Archie uses **Done When** to evaluate whether spawned agents are on track, and **Current Status** to avoid re-doing completed work.

**During the session:** Archie appends to **Decisions Log** via a new MCP tool `update_brief` when significant choices are made (tech decisions, scope changes, merge approvals).

**At shutdown:** `close_project` requires Archie to rewrite the **Current Status** section before the process exits, summarizing what was accomplished and what remains.

**`arch init`** scaffolds a blank `BRIEF.md` alongside `arch.yaml`.

### New MCP tool — available to Archie only

```
update_brief
  description: "Update a section of BRIEF.md. Use for Decisions Log entries and Current Status updates."
  params:
    section: enum           # "current_status" | "decisions_log"
    content: string         # For current_status: full replacement text.
                            # For decisions_log: one new row appended (date auto-injected).
  returns: { ok: bool }
```

---

## arch.yaml Schema

```yaml
project:
  name: string                  # Human-readable project name
  description: string           # Brief project description passed to Archie
  repo: string                  # Path to git repo (default: current directory)

archie:
  persona: string               # Path to Archie's persona file (default: personas/archie.md)
  model: string                 # Any Claude model ID (default: claude-opus-4-5)

agent_pool:                     # Available agent types (Archie spawns from this pool)
  - id: string                  # Unique type identifier, e.g. "frontend-dev"
    persona: string             # Path to persona .md file
    model: string               # Any Claude model ID (default: claude-sonnet-4-6)
    max_instances: int          # Max concurrent instances of this type (default: 1)

    # Sandbox / container settings (optional)
    sandbox:
      enabled: bool             # Run this agent in a Docker container (default: false)
      image: string             # Docker image with claude CLI installed (default: "arch-agent:latest")
      extra_mounts: [string]    # Additional host paths to mount as read-only, e.g. ["/usr/local/lib"]
      network: string           # Docker network mode: "bridge" (default) | "none" | "host"
      memory_limit: string      # e.g. "2g" (default: no limit)
      cpus: float               # e.g. 1.5 (default: no limit)

    # Permission settings (optional)
    permissions:
      skip_permissions: bool    # Run claude with --dangerously-skip-permissions (default: false)
                                # ANY agent with this set to true will trigger a user confirmation
                                # prompt at arch startup before any agent is spawned.

github:                         # Optional. If omitted, GitHub tools are disabled.
  repo: string                  # GitHub repo in "owner/repo" format, e.g. "acme/moscow-rules"
  default_branch: string        # Branch PRs merge into (default: "main")
  labels:                       # Labels to auto-create on repo init (arch init --github)
    - name: "agent:archie"
      color: "7057ff"
    - name: "phase:0"
      color: "0075ca"
    # etc — one label per agent role and phase defined in agent_pool
  issue_template: string        # Path to issue body template (default: built-in template)

settings:
  max_concurrent_agents: int    # Hard cap on total active agents (default: 5)
  state_dir: string             # Where to write state files (default: ./state)
  mcp_port: int                 # Port for local MCP server (default: 3999)
  token_budget_usd: float       # Optional: warn/stop when total cost exceeds this
  auto_merge: bool              # Archie can auto-merge worktrees without user approval (default: false)
  require_user_approval:        # Which Archie actions require user confirmation
    - merge                     # Require approval before git merge/PR
    - teardown_all              # Require approval before shutting down all agents
```

---

## Component Specifications

### 1. Orchestrator (`arch/orchestrator.py`)

Responsible for the full lifecycle of the ARCH system.

**Startup sequence:**
1. Parse and validate `arch.yaml`
2. Initialize state store (load existing `state/` or create fresh)
3. Verify git repo is accessible and clean enough to worktree
4. **Permission gate:** If any agent in `agent_pool` has `permissions.skip_permissions: true`, print a prominent warning listing the affected agents and require explicit user confirmation (`y`) before continuing. Log this acknowledgment with timestamp to `state/`.
5. **Container gate:** If any agent has `sandbox.enabled: true`, verify Docker daemon is running (`docker info`). Pull or build required images. Fail fast with a clear error if Docker is unavailable.
6. **GitHub gate:** If `github.repo` is set, run `gh auth status` and `gh repo view {repo}` to verify access. Warn (don't fail) if `gh` is not installed or not authenticated — GitHub tools will be disabled for the session.
6. Start MCP server on configured port
7. Create Archie's worktree: `{repo}/.worktrees/archie/`
8. Write Archie's CLAUDE.md into its worktree (persona file + injected harness context block)
9. Spawn Archie subprocess (see Session spec). Archie itself always runs locally, never in a container.
10. Start dashboard

**Shutdown sequence:**
1. Send shutdown signal to all active agent subprocesses
2. Wait for graceful exit (timeout: 30s), then force kill
3. Remove all worktrees unless `--keep-worktrees` flag passed
4. Persist final state to `state/`
5. Print cost summary to stdout

**Error handling:**
- Register `atexit` handler and `SIGINT`/`SIGTERM` handlers to ensure worktree cleanup even on crash
- If Archie subprocess exits unexpectedly, attempt `--resume` restart once before surfacing error to user

---

### 2. MCP Server (`arch/mcp_server.py`)

A local MCP server running on `localhost:{mcp_port}`. All claude sessions connect to it. It is the sole communication channel between agents and between agents and the harness.

**Transport:** The harness runs a single MCP HTTP/SSE server on `localhost:{mcp_port}`. Each `claude` process connects directly to it using the SSE transport. The `agent_id` is embedded in the URL path so the server knows which agent is calling — no proxy shim required.

**Generated MCP config** (written per-agent to `state/{agent_id}-mcp.json` at spawn time):
```json
{
  "mcpServers": {
    "arch": {
      "type": "sse",
      "url": "http://localhost:{mcp_port}/sse/{agent_id}"
    }
  }
}
```

**Server implementation:** Use the official `mcp` Python SDK's SSE server mode. The server extracts `agent_id` from the URL path on each request to identify the calling agent and enforce tool access controls (Archie vs worker).

#### MCP Tools — Available to ALL agents

```
send_message
  description: "Send a message to another agent or to Archie"
  params:
    to: string          # agent_id of recipient, "archie", or "broadcast"
    content: string     # message body
  returns: { message_id: string, timestamp: string }

get_messages
  description: "Retrieve messages addressed to you"
  params:
    since_id: string?   # optional: only return messages newer than this ID.
                        # If omitted, the harness automatically uses the last persisted
                        # cursor from state/archie-cursor.json so Archie never re-reads
                        # messages after a compaction or restart.
  returns: { messages: [{ id, from, to, content, timestamp, read }], cursor: string }
  # The harness persists the returned cursor to state/archie-cursor.json after every call.

update_status
  description: "Report your current task and status to the harness (shown in dashboard)"
  params:
    task: string        # what you are currently doing
    status: enum        # "idle" | "working" | "blocked" | "waiting_review" | "done"
  returns: { ok: bool }

report_completion
  description: "Signal that your assigned work is complete"
  params:
    summary: string     # what was accomplished
    artifacts: [string] # list of files created or modified
  returns: { ok: bool }

save_progress
  description: "Persist structured session state for continuity across context compactions and restarts.
                Call periodically during long tasks and before signaling completion."
  params:
    files_modified: [string]   # files created or changed this session
    progress: string           # what has been accomplished so far
    next_steps: string         # what remains to be done
    blockers: string?          # current blockers, if any
    decisions: [string]?       # architectural/scope decisions made this session
  returns: { ok: bool }
  # Stored in StateStore agents.json under the agent's "context" field.
  # On agent resume/restart, the orchestrator injects this into the agent's
  # CLAUDE.md as a "## Session State" section so the new session has full continuity.
  # See: https://github.com/AppSecHQ/arch/issues/1
```

#### MCP Tools — Available to Archie ONLY

Identity enforced by checking `agent_id == "archie"` in the MCP server.

```
spawn_agent
  description: "Spawn a new agent from the configured agent pool"
  params:
    role: string              # must match an id in agent_pool config
    assignment: string        # task description given to agent at spawn
    context: string?          # optional additional context injected into agent's CLAUDE.md
    skip_permissions: bool?   # request --dangerously-skip-permissions for this agent.
                              # ONLY valid if the role has permissions.skip_permissions: true
                              # in arch.yaml. If role does not have it configured, this
                              # param is ignored and skip_permissions is always false.
                              # The harness will surface an escalate_to_user confirmation
                              # if Archie requests this and it was not pre-approved at startup.
  returns: { agent_id: string, worktree_path: string, sandboxed: bool, skip_permissions: bool, status: "spawning" }

teardown_agent
  description: "Shut down an agent and remove its worktree"
  params:
    agent_id: string
    reason: string?
  returns: { ok: bool }

list_agents
  description: "Get current status of all active agents"
  returns:
    agents: [{
      id: string,
      role: string,
      status: string,
      task: string,
      tokens_used: int,
      cost_usd: float
    }]

escalate_to_user
  description: "Surface a question or decision to the human user. BLOCKS until answered."
  params:
    question: string       # question shown in dashboard
    options: [string]?     # optional list of choices (user can also type freely)
  returns: { answer: string }

request_merge
  description: "Request merging an agent's worktree branch into target branch"
  params:
    agent_id: string       # whose worktree to merge
    target_branch: string  # merge destination (default: main)
    pr_title: string?      # if provided, creates a GitHub PR instead of local merge
    pr_body: string?
  returns: { status: "approved" | "rejected" | "pending", pr_url: string? }

get_project_context
  description: "Get current project state: repo info, active agents, git status, and full BRIEF.md contents"
  returns: { name, description, repo_path, active_agents, git_status, open_worktrees, brief: string }

close_project
  description: "Signal that the project work is complete. Initiates graceful shutdown."
  params:
    summary: string
  returns: { ok: bool }
```

#### MCP Tools — GitHub Integration (Archie only)

These tools wrap the `gh` CLI. Require `gh` to be installed and authenticated (`gh auth status`). All tools are no-ops if `github.repo` is not set in `arch.yaml`. Archie uses these as a **Scrum Master** — creating issues for each task, tracking sprint progress, and closing issues via PRs.

```
gh_create_issue
  description: "Create a GitHub issue. Use for every discrete task assigned to an agent."
  params:
    title: string
    body: string             # Use the standard issue template: Context, Acceptance Criteria, Depends On, Agent Assignment
    labels: [string]?        # e.g. ["agent:frontend-dev", "phase:1", "type:feature"]
    milestone: string?       # Sprint milestone title, e.g. "Sprint 1"
    assignee: string?        # GitHub username, if known
  returns: { issue_number: int, url: string }

gh_list_issues
  description: "List GitHub issues with optional filters. Use to check sprint status and find blocked work."
  params:
    labels: [string]?        # Filter by label(s)
    milestone: string?       # Filter by milestone
    state: enum?             # "open" | "closed" | "all" (default: "open")
    limit: int?              # Max results (default: 30)
  returns: { issues: [{ number, title, labels, state, assignee, url }] }

gh_close_issue
  description: "Close a GitHub issue, optionally referencing the PR that resolves it."
  params:
    issue_number: int
    comment: string?         # Closing comment, e.g. "Resolved in PR #42"
  returns: { ok: bool }

gh_update_issue
  description: "Update an issue's labels, milestone, or assignee. Use to reflect status changes."
  params:
    issue_number: int
    add_labels: [string]?
    remove_labels: [string]?
    milestone: string?
    assignee: string?
  returns: { ok: bool }

gh_add_comment
  description: "Add a comment to a GitHub issue. Use for progress updates, blockers, or handoff notes."
  params:
    issue_number: int
    body: string
  returns: { ok: bool }

gh_create_milestone
  description: "Create a GitHub milestone representing a sprint or phase."
  params:
    title: string            # e.g. "Sprint 1" or "Phase 0 — Scaffold"
    description: string?
    due_date: string?        # ISO 8601 date, e.g. "2026-03-07"
  returns: { milestone_number: int, url: string }

gh_list_milestones
  description: "List open GitHub milestones (sprints/phases)."
  returns: { milestones: [{ number, title, open_issues, closed_issues, due_date, url }] }
```

**Implementation note:** Each tool shells out to `gh` CLI commands. Examples:
```python
# gh_create_issue
subprocess.run(["gh", "issue", "create", "--title", title, "--body", body, "--label", ",".join(labels)])

# gh_list_issues
subprocess.run(["gh", "issue", "list", "--label", ",".join(labels), "--json", "number,title,labels,state,assignee,url"])

# gh_close_issue
subprocess.run(["gh", "issue", "close", str(issue_number), "--comment", comment])
```

---

### 3. Session Manager (`arch/session.py`)

Manages the lifecycle of a single `claude` CLI subprocess, either running locally or inside a Docker container. Delegates container logic to `container.py`.

**Local spawn command:**
```python
claude_cmd = [
    "claude",
    "--model", agent_config.model,
    "--output-format", "stream-json",   # enables structured token tracking
    "--mcp-config", mcp_config_path,    # path to generated MCP config JSON
    "--print",                          # non-interactive / headless mode
]

if agent_config.permissions.skip_permissions:
    claude_cmd += ["--dangerously-skip-permissions"]

claude_cmd += [spawn_prompt]
```

If resuming an existing session:
```python
claude_cmd += ["--resume", session_id]
```

**Container spawn:** When `sandbox.enabled: true`, the session manager delegates to `container.py` instead of spawning a local subprocess directly. See Container Manager spec below.

**Permission flag logging:** Any time `--dangerously-skip-permissions` is used, log a timestamped entry to `state/permissions_audit.log`:
```
2026-02-24T14:01:33Z  SKIP_PERMISSIONS  agent_id=security-1  role=security  approved_by=user
```

**Output parsing:**
Read stdout line by line. Each line is a JSON object. Relevant event types:

```json
{ "type": "assistant", "message": { "content": [...] } }     // agent output text
{ "type": "usage", "input_tokens": N, "output_tokens": N, "cache_read_input_tokens": N, "cache_creation_input_tokens": N }
{ "type": "result", "session_id": "abc123" }                  // emitted at end; persist session_id for resume
```

**Session ID persistence:** On subprocess exit, parse the `result` event and save `session_id` to `state/agents.json` under the agent's entry.

**Unexpected exit handling:** If process exits with non-zero code, set agent status to `"error"` and send a message to Archie: `"Agent {agent_id} exited unexpectedly. Check state/agents.json for details."`

---

### 5. Container Manager (`arch/container.py`)

Handles Docker-based agent isolation. Called by the Session Manager when `sandbox.enabled: true`.

**How it works:**

The agent's worktree is mounted into the container as a volume. The claude CLI runs inside the container. The MCP proxy runs on the host — the container reaches it via `host.docker.internal` (macOS/Windows) or the docker bridge gateway IP (Linux).

**Generated MCP config for containerized agents** (`state/{agent_id}-mcp.json`):
```json
{
  "mcpServers": {
    "arch": {
      "type": "sse",
      "url": "http://host.docker.internal:{mcp_port}/sse/{agent_id}"
    }
  }
}
```
The container reaches the host's MCP server via `host.docker.internal` (macOS/Windows) or the docker bridge gateway IP (Linux, set via `--add-host host.docker.internal:host-gateway`).

**Container spawn:**
```python
docker_cmd = [
    "docker", "run",
    "--rm",                                              # auto-remove on exit
    "--name", f"arch-{agent_id}",
    "-v", f"{worktree_path}:/workspace",                 # mount worktree
    "-v", f"{mcp_config_path}:/arch/mcp-config.json:ro",# mount MCP config read-only
    "-w", "/workspace",                                  # working directory
    "--add-host", "host.docker.internal:host-gateway",   # reach host MCP proxy (Linux)
    "-e", f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}",  # pass API key
]

# Apply resource limits from config
if agent_config.sandbox.memory_limit:
    docker_cmd += ["--memory", agent_config.sandbox.memory_limit]
if agent_config.sandbox.cpus:
    docker_cmd += ["--cpus", str(agent_config.sandbox.cpus)]
if agent_config.sandbox.network == "none":
    docker_cmd += ["--network", "none"]
if agent_config.sandbox.extra_mounts:
    for mount in agent_config.sandbox.extra_mounts:
        docker_cmd += ["-v", f"{mount}:{mount}:ro"]

docker_cmd += [agent_config.sandbox.image]

# Then the claude command runs as the container entrypoint
claude_cmd = ["claude", "--model", ..., "--mcp-config", "/arch/mcp-config.json", "--print", ...]
if agent_config.permissions.skip_permissions:
    claude_cmd += ["--dangerously-skip-permissions"]
```

**Required Docker image:** The image must have the `claude` CLI installed and authenticated, or authentication must be passed via env var. Provide a default `Dockerfile` in the repo:

```dockerfile
FROM python:3.11-slim
RUN pip install anthropic
RUN npm install -g @anthropic-ai/claude-code
WORKDIR /workspace
ENTRYPOINT []
```

**Teardown:** `docker stop arch-{agent_id}` — the `--rm` flag handles removal.

**State tracking:** The container name is stored in `state/agents.json` as `container_name` for each sandboxed agent.

**Dashboard indicator:** Containerized agents show a `[c]` tag next to their name in the agents panel.

---

### 6. Worktree Manager (`arch/worktree.py`)

Each agent works in an isolated git worktree.

**On `spawn_agent`:**
```bash
git worktree add .worktrees/{agent_id} -b agent/{agent_id}
```
Then write the agent's CLAUDE.md (persona + injected context block) to `.worktrees/{agent_id}/CLAUDE.md`.

**On `teardown_agent`:**
```bash
git worktree remove .worktrees/{agent_id} --force
# Optionally: git branch -d agent/{agent_id}
```

**On `request_merge` (approved, no PR):**
```bash
git checkout {target_branch}
git merge --no-ff agent/{agent_id} -m "Merge {agent_id}: {summary}"
```

**On `request_merge` (PR mode):**
```bash
gh pr create --title "{pr_title}" --body "{pr_body}" --head agent/{agent_id} --base {target_branch}
```

---

### 7. Token Tracker (`arch/token_tracker.py`)

Parses `stream-json` output events and accumulates per-agent cost.

**Per-agent data structure:**
```python
{
    "agent_id": "frontend-dev-1",
    "model": "claude-sonnet-4-6",
    "input_tokens": 45231,
    "output_tokens": 12847,
    "cache_read_tokens": 8000,
    "cache_creation_tokens": 2000,
    "turns": 14,
    "cost_usd": 0.287
}
```

**Pricing constants** (verify against https://anthropic.com/pricing before hardcoding):
```python
PRICING_PER_MILLION = {
    "claude-opus-4-5":   { "input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75 },
    "claude-opus-4-6":   { "input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75 },
    "claude-sonnet-4-6": { "input": 3.00,  "output": 15.00, "cache_read": 0.30,  "cache_write": 3.75  },
    "claude-sonnet-4-5": { "input": 3.00,  "output": 15.00, "cache_read": 0.30,  "cache_write": 3.75  },
    "claude-haiku-4-5":  { "input": 0.80,  "output": 4.00,  "cache_read": 0.08,  "cache_write": 1.00  },
}
# For unknown model IDs, fall back to Sonnet pricing and log a warning.
# The pricing table should be kept in a separate config file (pricing.yaml)
# so it can be updated without code changes as new models are released.
```

Cost calculation:
```python
cost = (
    (input_tokens / 1_000_000) * pricing["input"] +
    (output_tokens / 1_000_000) * pricing["output"] +
    (cache_read_tokens / 1_000_000) * pricing["cache_read"] +
    (cache_creation_tokens / 1_000_000) * pricing["cache_write"]
)
```

Persisted to `state/usage.json` after every parsed usage event.

---

### 8. State Store (`arch/state.py`)

Single source of truth. In-memory Python dict, flushed to `state/*.json` after every mutation.

```python
State = {
    "project": {
        "name": str,
        "description": str,
        "repo": str,
        "started_at": str       # ISO 8601 UTC
    },
    "agents": {
        "{agent_id}": {
            "id": str,
            "role": str,
            "status": str,      # idle|working|blocked|waiting_review|done|error
            "task": str,        # current task description
            "session_id": str,       # claude session ID for --resume
            "worktree": str,
            "pid": int,              # local process ID, or None if containerized
            "container_name": str,   # docker container name, or None if local
            "sandboxed": bool,
            "skip_permissions": bool,
            "spawned_at": str,
            "usage": { ... },        # from token_tracker
            "context": {             # persisted by save_progress MCP tool (see #1)
                "files_modified": [str],
                "progress": str,
                "next_steps": str,
                "blockers": str,     # null if none
                "decisions": [str]
            }
        }
    },
    "messages": [
        {
            "id": str,
            "from": str,
            "to": str,
            "content": str,
            "timestamp": str,
            "read": bool
        }
    ],
    "pending_user_decisions": [
        {
            "id": str,
            "question": str,
            "options": [str],   # may be empty
            "asked_at": str,
            "answered_at": str, # null until answered
            "answer": str       # null until answered
        }
    ],
    "tasks": [
        {
            "id": str,
            "assigned_to": str,
            "description": str,
            "status": str,      # pending|in_progress|done
            "created_at": str,
            "completed_at": str # null until done
        }
    ]
}
```

---

### 9. Dashboard (`arch/dashboard.py`)

Built with `textual`. Refreshes every 2 seconds.

**Layout:**
```
┌─────────────────────────────────────────────────────────────────────┐
│  ARCH  ·  ProjectName  ·  Runtime: 00:14:32      [q]uit  [?]help   │
├───────────────┬──────────────────────────────────┬──────────────────┤
│ AGENTS        │ ACTIVITY LOG                     │ COSTS            │
│               │                                  │                  │
│ ● archie      │ 14:01 archie   Spawning fe-dev-1 │ archie   $0.12   │
│   Coordinating│ 14:02 fe-dev   Starting NavBar   │ fe-dev   $0.04   │
│               │ 14:03 qa-1     Running tests      │ qa-1     $0.02   │
│ ●[c] fe-dev-1 │ 14:04 fe-dev   BLOCKED: needs API│ sec      $0.01   │
│   Building    │ 14:05 archie   Checking in        │ ──────────────   │
│   NavBar      │                                  │ Total    $0.19   │
│               │                                  │ Budget   $5.00   │
│ ● qa-1        │                                  │ ████░░   3.8%    │
│   Running     │                                  │                  │
│   tests       │                                  │                  │
│               │                                  │                  │
│ ●[c][!] sec-1 │                                  │                  │
│   Auditing    │                                  │                  │
├───────────────┴──────────────────────────────────┴──────────────────┤
│ ⚠ ARCHIE ASKS: Merge frontend-dev-1 worktree to main? [y/N]: _     │
└─────────────────────────────────────────────────────────────────────┘
```

**Agent status indicators:**
- `●` green — actively working
- `●` yellow — blocked or waiting for review
- `○` grey — idle
- `✓` green — done
- `✗` red — error
- `[c]` tag — agent is running in a Docker container (sandboxed)
- `[!]` tag — agent is running with `--dangerously-skip-permissions`

**Cost bar:** green → yellow at 75% of budget → red at 90%.

**Pending decisions:** Appear in the bottom panel. Dashboard awaits keyboard input. Sets the `asyncio.Event` in `mcp_proxy.py` which unblocks Archie's `escalate_to_user` call.

**Keyboard shortcuts:**
- `q` — graceful shutdown
- `?` — help overlay
- `l` — view Archie's full conversation log
- `1`–`9` — view individual agent conversation log
- `m` — view message bus log

---

### 10. Persona Files

Each persona is a markdown file written in CLAUDE.md style for that role. The harness injects a context block at the top when writing to each agent's worktree at spawn time.

**Injected header block:**
```markdown
<!-- INJECTED BY ARCH — DO NOT EDIT BELOW THIS LINE -->
## ARCH Harness Context
- **Your agent ID:** {agent_id}
- **Project:** {project_name} — {project_description}
- **Your worktree path:** {worktree_path}
- **Available MCP tools (via "arch" server):** send_message, get_messages, update_status, save_progress, report_completion
- **Active team members:** {comma-separated list of agent_id: role}
- **Your assignment:** {assignment}
<!-- END ARCH CONTEXT -->

---

{original persona file content}
```

**Archie's persona (`personas/archie.md`) must include instructions to:**

*Session startup:*
- Call `get_project_context` as first action — reads `BRIEF.md`, git status, and active agents
- Read **Done When** criteria — these define success
- Read **Current Status** — do not re-do completed work
- If GitHub is enabled, call `gh_list_milestones` and `gh_list_issues` to understand the current sprint state before spawning any agents

*Sprint / phase planning:*
- Act as Scrum Master: decompose the project into sprints using `gh_create_milestone` for each phase or time-box
- Create a GitHub issue for every discrete task via `gh_create_issue`, using the standard template (Context, Acceptance Criteria, Depends On, Agent Assignment)
- Label issues by agent role, phase, system, and type
- Respect dependency order — do not create implementation issues until blocker issues are resolved
- For long-running projects, plan one sprint at a time; do not pre-create all issues upfront

*During the session:*
- Call `update_brief` (section: current_status) as a checkpoint after every sprint milestone, significant merge, or `escalate_to_user` response — not only at shutdown. This ensures recovery points exist throughout long sessions.
- Spawn agents with `spawn_agent`, then assign them their GitHub issue number in the spawn prompt
- Agents should reference the issue number in commits (`closes #42`) and PRs
- Monitor progress via `list_agents`, `get_messages`, and `gh_list_issues`
- When an agent is blocked, add a comment via `gh_add_comment` and update the label to `blocked`
- Call `update_brief` (section: decisions_log) whenever a significant architectural or scope decision is made

*Completing work:*
- When an agent reports completion, verify against the issue's Acceptance Criteria
- Coordinate PR merge via `request_merge`; close the issue via `gh_close_issue` referencing the PR
- Use `escalate_to_user` for decisions requiring human judgement (game feel, design choices, merge conflicts)
- At end of sprint, call `gh_list_milestones` to report sprint velocity to the user

*Session shutdown:*
- Call `update_brief` (section: current_status) summarising what was done, what's in progress, and what's next
- Call `close_project` to initiate graceful shutdown

---

## CLI Entrypoint (`arch.py`)

```
Usage:
  arch up [--config arch.yaml] [--keep-worktrees]
        Start ARCH and launch Archie

  arch down
        Gracefully shut down all agents and clean up

  arch status
        Show current state of a running ARCH session

  arch resume
        Resume from saved state/ directory

  arch init [--name "My Project"] [--github owner/repo]
        Scaffold arch.yaml + personas/ + BRIEF.md in current directory.
        If --github is provided: creates labels and default milestones in the repo.
```

---

## Implementation Order

Build and verify each layer before building on it:

1. **State store** (`state.py`) — data model, in-memory dict, JSON flush/load, unit tests
2. **Worktree manager** (`worktree.py`) — create/list/remove worktrees; test against a real git repo
3. **Token tracker** (`token_tracker.py`) — parse stream-json fixtures, verify cost calculation, unit tests
4. **MCP server** (`mcp_server.py`) — SSE/HTTP server using `mcp` SDK; implement all tools; extract `agent_id` from URL path; test each tool in isolation
5. **Session manager — local** (`session.py`) — spawn claude subprocess locally, read stream-json output, persist session_id, permissions audit log
6. **Container manager** (`container.py`) — Docker spawn/teardown, volume mounts, `host.docker.internal` MCP connectivity; test with a real Docker image
7. **Session manager — container** — integrate container.py into session.py, unified interface regardless of mode
8. **Orchestrator** (`orchestrator.py`) — wire all components, startup/shutdown, permission gate, container gate, GitHub gate, signal handlers
9. **Dashboard** (`dashboard.py`) — Textual layout, live state binding, `[c]`/`[!]` indicators, user input for decisions
10. **Persona files** — write default personas for: archie, frontend, backend, qa, security, copywriter
11. **GitHub tools** — implement all `gh_*` MCP tools as `gh` CLI wrappers; test against a real repo; handle missing `gh` gracefully
11.5. **Agent state persistence** — `context` field in StateStore, `save_progress` MCP tool, CLAUDE.md injection on resume/restart. See [#1](https://github.com/AppSecHQ/arch/issues/1).
12. **CLI entrypoint** (`arch.py`) — argument parsing, `init` scaffold command, `--github` flag for label/milestone setup, default Dockerfile
13. **Integration test** — end-to-end run against a real git repo: Archie + 1 local agent + 1 sandboxed agent + GitHub Issues enabled. Should exercise agent state persistence (spawn → save_progress → teardown → verify context persisted).

---

## Key Constraints and Edge Cases

- **Never** store or log API keys or credentials anywhere in `state/` or logs. The `ANTHROPIC_API_KEY` is passed to containers via env var at runtime only and never written to disk.
- **`--dangerously-skip-permissions` must never be used silently.** It must be declared in `arch.yaml` AND confirmed by the user at startup. Any runtime request from Archie to use it on an undeclared role must trigger an `escalate_to_user` before spawning. Log all uses to `state/permissions_audit.log`.
- **Archie itself never runs with `--dangerously-skip-permissions`** and never runs in a container. Archie runs on the host with standard permissions always.
- **Container teardown:** Use `--rm` on `docker run` so containers self-remove. Also register `docker stop arch-{agent_id}` in the `atexit` handler for each containerized agent in case `--rm` doesn't fire cleanly.
- **Container networking:** On Linux, `host.docker.internal` is not available by default — use `--add-host host.docker.internal:host-gateway` in the docker run command.
- **Never** store or log API keys or credentials. `claude` CLI handles auth natively.
- Worktrees must be cleaned up even on crash — register both `atexit` and `SIGINT`/`SIGTERM` handlers in the orchestrator.
- If an agent subprocess exits unexpectedly (non-zero exit), mark it `"error"` and notify Archie via the message bus.
- If Archie sends `send_message` to an `agent_id` that hasn't been spawned yet, queue the message — deliver it when that agent comes online.
- `escalate_to_user` must block Archie's MCP call until the user responds. Implement as an `asyncio.Event` on the MCP server — the SSE handler awaits the event, which the dashboard's keyboard handler sets when the user answers.
- Dashboard must remain responsive even if an agent subprocess is hanging. Use non-blocking subprocess stdout reads (`asyncio.create_subprocess_exec`).
- Merges must always use `--no-ff` to preserve branch history and attribution.
- All timestamps stored as ISO 8601 UTC strings.
- The `state/` directory should be gitignored. The `arch init` command should add it automatically.
- Token budget enforcement: if `token_budget_usd` is set and total cost exceeds it, surface an `escalate_to_user` asking whether to continue.

---

## Out of Scope (v1)

- Web UI (dashboard is TUI only)
- Multi-repo projects
- Remote agents (all agents run on local machine)
- Agent-to-agent direct file transfer (use git worktree merge path)
- Plugin system for custom MCP tools
- Nested agent teams (agents cannot spawn sub-agents)
- Kubernetes / container orchestration (Docker only)
- Custom seccomp/AppArmor profiles for containers (use Docker defaults)
