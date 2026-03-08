# ARCH — Agent Runtime & Coordination Harness

> Meet **Archie** — your AI development team lead.

ARCH is a multi-agent development system that orchestrates independent Claude AI sessions working concurrently on a software project. Each agent is a full Claude CLI process with its own role, memory, and isolated git worktree. A central harness connects them via a local MCP server, tracks token costs, and renders a live terminal dashboard.

---

## How It Works

```
archie up          # start the orchestrator (terminal 1)
archie dashboard   # open the live dashboard (terminal 2)
```

Archie (the Lead Agent) reads your project brief, analyzes the scope, and proposes a team of specialist agents — frontend dev, QA engineer, security auditor, and more. You approve the team plan from the dashboard, and Archie spawns them to work in parallel across isolated git branches. You watch progress in real time, send messages to Archie, answer questions, and review results as work completes.

---

## Features

- **Dynamic team planning** — Archie reads BRIEF.md, scans available personas, and proposes the right team for the project
- **User approval** — team plans are escalated for your sign-off before agents are spawned (configurable auto-approve)
- **Isolated git worktrees** — agents work in parallel without filesystem conflicts
- **Agent-to-agent messaging** — agents coordinate via a local MCP message bus
- **Token & cost tracking** — per-agent usage tracked from Claude CLI stream output, displayed in real time
- **MCP event log** — every tool call logged with timing to `state/events.jsonl`, viewable in dashboard
- **Sandboxed agents** — run agents in Docker containers for safety and isolation
- **Permission control** — three-layer system: auto-approve common tools, whitelist via config, runtime approval via dashboard
- **Live TUI dashboard** — agent status, activity log, costs, event history, and interactive messaging
- **Always-on input** — send messages to Archie anytime from the dashboard or CLI
- **Auto-merge safety** — unmerged agent work is auto-merged before worktree cleanup
- **Configurable** — single `arch.yaml` defines your project, settings, and optional pre-configured agent pool

---

## Dashboard

ARCH includes a live terminal dashboard built with [Textual](https://textual.textualize.io/) that shows you everything happening across your agent team in real time.

### Main View

The main dashboard displays four panels: **Agents** (status and current task), **Activity Log** (inter-agent messages as they happen), **Costs** (per-agent token spend and budget), and an **Input Bar** for sending messages to Archie or answering escalations.

![ARCH Dashboard — Main View](docs/dashboard-main.png)

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `?` | Help screen |
| `l` | View Archie's sent messages |
| `m` | View full message bus |
| `e` | View MCP tool call event history |
| `1-9` | View individual agent messages |
| Enter | Send message to Archie (or answer escalation) |

### Message Log

Press `m` to open the full message bus showing all inter-agent communication with timestamps and senders.

![ARCH Dashboard — Message Log](docs/dashboard-messages.png)

### MCP Event Log

Press `e` to see the full history of MCP tool calls — which agent called what, with arguments, result status, and duration. Useful for debugging and understanding agent behavior.

---

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Scaffold config in your project
archie init --name "My Project"

# Write your project brief
# Edit BRIEF.md with your project goals, constraints, and "Done When" criteria

# Start the orchestrator
archie up

# In another terminal, open the dashboard
archie dashboard
```

---

## Communicating with Archie

You can send messages to Archie while the system is running:

```bash
# From the dashboard — type in the input bar and press Enter

# From the CLI
archie send "Please prioritize the API endpoints over the UI"
```

Archie picks up messages on the next `get_messages` call. If Archie's session has ended, the orchestrator's auto-resume detects unread messages and restarts the session.

---

## Configuration

### Minimal config (Archie plans the team dynamically)

```yaml
# arch.yaml
project:
  name: My App
  description: A full-stack web application

settings:
  max_concurrent_agents: 5
  token_budget_usd: 10.00
```

With no `agent_pool` defined, Archie will:
1. Read your BRIEF.md
2. Scan available personas (from `personas/` or `agents/` directories)
3. Propose a team via `plan_team`
4. Escalate the plan for your approval
5. Spawn the approved agents

### Full config (pre-configured agent pool)

```yaml
# arch.yaml
project:
  name: My App
  description: A full-stack web application

agent_pool:
  - id: frontend-dev
    persona: personas/frontend.md
    model: claude-sonnet-4-6
  - id: qa-engineer
    persona: personas/qa.md
    model: claude-sonnet-4-6
    sandbox:
      enabled: true
  - id: security-auditor
    persona: personas/security.md
    model: claude-sonnet-4-6
    sandbox:
      enabled: true
    permissions:
      skip_permissions: true

settings:
  max_concurrent_agents: 5
  token_budget_usd: 10.00
  auto_approve_team: false   # set true to skip team plan approval
  auto_merge: false
```

### Personas

Personas are Markdown files that define an agent's role, expertise, and working style. ARCH ships with built-in personas:

| Persona | File | Description |
|---------|------|-------------|
| Frontend Developer | `personas/frontend.md` | UI and client-side applications |
| Backend Developer | `personas/backend.md` | Server-side, APIs, and data systems |
| QA Engineer | `personas/qa.md` | Testing and quality assurance |
| Security Auditor | `personas/security.md` | Security review and hardening |
| Copywriter | `personas/copywriter.md` | Documentation and content |

Place custom personas in your project's `personas/` or `agents/` directory. Archie discovers them automatically via `list_personas`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    archie up                             │
│                                                         │
│  ┌──────────┐   ┌────────────┐   ┌──────────────────┐  │
│  │Orchestrator│──│ MCP Server │──│  State Store       │  │
│  │          │   │ (SSE/HTTP) │   │  state/*.json      │  │
│  │          │   │            │   │  events.jsonl      │  │
│  └──────────┘   └────────────┘   └──────────────────┘  │
│       │              │                    │              │
│  ┌────┴────┐    ┌────┴────┐         ┌────┴────┐        │
│  │ Archie  │    │Worker 1 │   ...   │Worker N │        │
│  │(claude) │    │(claude) │         │(claude) │        │
│  │worktree │    │worktree │         │worktree │        │
│  └─────────┘    └─────────┘         └─────────┘        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                 archie dashboard                         │
│                                                         │
│  Reads state/*.json files, posts escalation answers     │
│  via HTTP to /api/escalation/{id}                       │
└─────────────────────────────────────────────────────────┘
```

### Key Components

- **Orchestrator** — lifecycle management, agent spawning/teardown, auto-resume, signal handling
- **MCP Server** — SSE/HTTP server providing tools to agents (messaging, spawning, merging, escalation)
- **State Store** — thread-safe JSON persistence for agents, messages, decisions, project status
- **Token Tracker** — parses Claude CLI stream-json output for per-agent cost tracking
- **Session Manager** — manages claude CLI subprocesses (local or containerized)
- **Worktree Manager** — git worktree creation, merge, PR creation, cleanup
- **Dashboard** — Textual TUI running as a separate process, reads state files

### MCP Tools

Agents communicate with the orchestrator through MCP tools:

**All agents:**
`send_message`, `get_messages`, `update_status`, `report_completion`, `save_progress`

**Archie only:**
`spawn_agent`, `teardown_agent`, `list_agents`, `escalate_to_user`, `request_merge`, `get_project_context`, `close_project`, `update_brief`, `list_personas`, `plan_team`

**GitHub (Archie, when configured):**
`gh_create_issue`, `gh_list_issues`, `gh_close_issue`, `gh_update_issue`, `gh_add_comment`, `gh_create_milestone`, `gh_list_milestones`

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `archie up` | Start the orchestrator |
| `archie dashboard` | Open the live TUI dashboard |
| `archie send "msg"` | Send a message to Archie |
| `archie status` | Show project status and costs |
| `archie down` | Stop a running orchestrator |
| `archie init` | Scaffold arch.yaml and BRIEF.md |

---

## FAQ

### What is ARCH for?

ARCH is for anyone who wants to throw a team of AI agents at a software project instead of doing everything in a single chat session. You describe what you want built, and Archie — the lead agent — breaks the work down, proposes a team, and coordinates specialists working in parallel across isolated git branches. You supervise from a dashboard, answer questions, and approve merges.

### What kinds of projects can I build with this?

Anything you'd assign to a small dev team:

- **Full-stack web apps** — Archie assigns frontend and backend agents to work simultaneously, with QA writing tests in parallel
- **Security audits & scanning tools** — dedicated security agents review code while others build features
- **Refactoring & migration projects** — multiple agents work through different modules concurrently
- **MVPs and prototypes** — go from idea to deployed app with agents handling design, implementation, and testing
- **Documentation & content projects** — copywriter agents draft docs while devs build the thing being documented

The sweet spot is projects with parallelizable work — tasks that a human team would split across 2-5 people.

### How is this different from just using Claude Code?

Claude Code is a single agent — one session, one context window, one thread of work. ARCH runs *multiple* Claude Code sessions simultaneously, each with a dedicated role, its own git worktree, and a shared message bus for coordination. Think of it as the difference between one developer and a team.

### Do I need Docker?

No. Agents run as local processes by default. Docker sandboxing is opt-in per agent for additional isolation.

### How much does it cost to run?

ARCH tracks token usage and costs per agent in real time on the dashboard. Costs depend on how many agents you spawn, which models you use, and how complex the project is. Set `token_budget_usd` in `arch.yaml` to cap spend.

### Can I talk to Archie while it's running?

Yes. Type in the dashboard input bar or use `archie send "your message"` from the CLI. Archie picks up messages automatically. If Archie's session has ended, the orchestrator restarts it to handle the message.

---

## Status

Active development. See [SPEC-AGENT-HARNESS.md](./SPEC-AGENT-HARNESS.md) for the full technical specification.
