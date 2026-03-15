#!/bin/bash
#
# UAT #5 Setup & Launch Script
# Creates arch-test-5 project and starts ARCH
#
# WHAT THIS TESTS:
#   1. Archie reads BRIEF.md and plans work
#   2. Archie spawns agent(s) and assigns tasks
#   3. Agent(s) build the requested feature
#   4. Agent(s) report completion
#   5. Archie merges completed work to main
#   6. Archie tears down agents
#   7. Dashboard shows all of the above in real-time
#   8. Graceful shutdown (q key or Ctrl+C)
#
set -e

UAT_DIR="$HOME/claude-projects/arch-test-5"
ARCH_DIR="$HOME/claude-projects/arch"

echo "=== ARCH UAT #5 Setup ==="
echo ""

# 1. Clean up any previous run
if [ -d "$UAT_DIR" ]; then
    echo "Removing previous arch-test-5..."
    rm -rf "$UAT_DIR"
fi

# 2. Create project directory
echo "Creating $UAT_DIR..."
mkdir -p "$UAT_DIR"
cd "$UAT_DIR"

# 3. Initialize git repo
echo "Initializing git repo..."
git init
git config user.email "uat@arch-test.com"
git config user.name "UAT Tester"

# 4. Create personas
mkdir -p personas

cat > personas/archie.md << 'PERSONA'
# Archie — Lead Agent

You are **Archie**, the Lead Agent for ARCH.

## Session Startup

1. Call `get_project_context` as your **first action**.
2. Read the BRIEF.md goals and "Done When" criteria.
3. Spawn specialist agent(s) to do the work.

## Spawning Agents

Use `spawn_agent` to create workers:
```
spawn_agent(role: "frontend", assignment: "Build the todo app...")
```

Be specific about what you need built. Include acceptance criteria.

## Monitoring

- Call `list_agents` to check agent status
- Call `get_messages` to read agent messages

## Completing Work

When an agent calls `report_completion`:
1. Review their summary
2. Merge their work: `request_merge(agent_id: "frontend-1", target_branch: "main")`
3. Tear down the agent: `teardown_agent(agent_id: "frontend-1")`

## Session Shutdown

When all "Done When" criteria are met:
1. Call `close_project(summary: "All criteria met.")`

## IMPORTANT
- You are the coordinator, NOT the implementer
- Always spawn agents to do the actual coding
- Always merge completed work back to main
- Always tear down agents when their work is merged
PERSONA

cat > personas/frontend.md << 'PERSONA'
# Frontend Developer

You build user interfaces with HTML, CSS, and JavaScript.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- If blocked, set status to "blocked" and message Archie

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you built", artifacts: ["file1.html"])`
PERSONA

# 5. Create BRIEF.md — simple todo app (quick to build, easy to verify)
cat > BRIEF.md << 'BRIEF'
# Todo App

## Goals

Build a simple todo list web application.

## Done When

- [ ] Single HTML file (index.html) with embedded CSS and JS
- [ ] User can add a new todo item via text input and "Add" button
- [ ] User can mark a todo item as complete (checkbox or click)
- [ ] User can delete a todo item
- [ ] Completed items are visually distinct (strikethrough)
- [ ] Looks clean and modern (centered layout, decent typography)

## Constraints

- Single HTML file, no frameworks, no build tools
- Must work by opening the file directly in a browser

## Current Status

Not started.

## Decisions Log

| Date | Decision |
|------|----------|
BRIEF

# 6. Create arch.yaml — simpler config, just 1 agent type
cat > arch.yaml << YAML
project:
  name: "Todo App"
  description: "Simple todo list web application"
  repo: "."

archie:
  persona: "personas/archie.md"
  model: "claude-opus-4-6"

agent_pool:
  - id: frontend
    persona: "personas/frontend.md"
    model: "claude-sonnet-4-6"
    max_instances: 1

settings:
  max_concurrent_agents: 2
  state_dir: "./state"
  mcp_port: 3999
  token_budget_usd: 5.00
YAML

# 7. Add .gitignore
cat > .gitignore << 'GI'
state/
.worktrees/
GI

# 8. Initial commit
git add .
git commit -m "Initial project setup for UAT #5"

echo ""
echo "=== Project created at $UAT_DIR ==="
echo ""
echo "Contents:"
ls -la
echo ""
echo "BRIEF.md:"
cat BRIEF.md
echo ""
echo "=== Launching ARCH ==="
echo ""
echo "Dashboard opens automatically at http://localhost:3999/dashboard"
echo ""
echo "CHECKLIST (watch for these in the web dashboard):"
echo "  [ ] Dashboard shows agents panel with Archie"
echo "  [ ] A frontend agent gets spawned (appears in agents panel)"
echo "  [ ] Activity log shows messages between agents"
echo "  [ ] Frontend agent status changes to 'working'"
echo "  [ ] Frontend agent status changes to 'done'"
echo "  [ ] Work gets merged to main (check git log after)"
echo "  [ ] Agent gets torn down"
echo "  [ ] close_project confirmation in dashboard"
echo ""
echo "AFTER SHUTDOWN, VERIFY:"
echo "  cd $UAT_DIR"
echo "  git log --oneline          # Should show merge commit(s)"
echo "  git branch                  # Should be on main"
echo "  ls .worktrees/ 2>/dev/null  # Should be empty or not exist"
echo "  cat index.html              # Should be the todo app"
echo "  open index.html             # Should work in browser"
echo ""
echo "Press Enter to launch..."
read

source "$ARCH_DIR/.venv/bin/activate"
python "$ARCH_DIR/arch.py" up
