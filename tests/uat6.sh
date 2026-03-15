#!/bin/bash
#
# UAT #6 Setup & Launch Script
# Creates arch-test-6 project and starts ARCH
#
# WHAT THIS TESTS (multi-agent coordination):
#   1. Archie reads BRIEF.md and plans work across 2+ agents
#   2. Archie spawns BOTH frontend and qa agents
#   3. Frontend agent builds the web app
#   4. QA agent writes validation tests
#   5. Both agents report completion independently
#   6. Archie auto-resumes, merges BOTH branches to main
#   7. Archie tears down both agents
#   8. Dashboard shows parallel agent activity
#   9. Dashboard stays open after completion (press q to exit)
#
set -e

UAT_DIR="$HOME/claude-projects/arch-test-6"
ARCH_DIR="$HOME/claude-projects/arch"

echo "=== ARCH UAT #6 Setup (Multi-Agent) ==="
echo ""

# 1. Clean up any previous run
if [ -d "$UAT_DIR" ]; then
    echo "Removing previous arch-test-6..."
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
3. Plan the work and spawn specialist agents.

## Spawning Agents

Use `spawn_agent` to create workers. For this project you should spawn
**both** a frontend agent and a qa agent so they work in parallel:

```
spawn_agent(role: "frontend", assignment: "Build the web app...")
spawn_agent(role: "qa", assignment: "Write validation tests...")
```

Be specific about what you need built. Include acceptance criteria.
Spawn both agents before waiting for either to finish.

## Monitoring

- Call `list_agents` to check agent status
- Call `get_messages` to read agent messages

## Completing Work

When an agent calls `report_completion`:
1. Review their summary
2. Merge their work: `request_merge(agent_id: "...", target_branch: "main")`
3. Tear down the agent: `teardown_agent(agent_id: "...")`

Wait for ALL agents to complete before closing.

## Session Shutdown

When all "Done When" criteria are met:
1. Call `close_project(summary: "All criteria met.")`

## IMPORTANT
- You are the coordinator, NOT the implementer
- Always spawn agents to do the actual coding
- Spawn BOTH agents so they work in parallel
- Merge each agent's work as it completes
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
- Commit your work to git frequently
- If blocked, set status to "blocked" and message Archie

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you built", artifacts: ["file1.html"])`
PERSONA

cat > personas/qa.md << 'PERSONA'
# QA Engineer

You write tests and validate that applications meet their requirements.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Write clear, runnable test scripts
- Commit your work to git frequently

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you tested", artifacts: ["test_file.py"])`
PERSONA

# 5. Create BRIEF.md — multi-page site (requires frontend + QA)
cat > BRIEF.md << 'BRIEF'
# Multi-Page Portfolio Site

## Goals

Build a simple multi-page portfolio website with validation tests.

## Done When

- [ ] index.html — Home page with name, tagline, and navigation links to other pages
- [ ] projects.html — Projects page listing 3 sample projects with title, description, and a link back to home
- [ ] contact.html — Contact page with a styled contact form (name, email, message fields) and link back to home
- [ ] Consistent navigation bar across all 3 pages
- [ ] Clean, modern CSS styling (can be inline or in a shared style block)
- [ ] All pages work by opening directly in a browser (no server needed)
- [ ] test_site.py — Python test script that validates:
  - All 3 HTML files exist
  - Each file contains a nav element or navigation links
  - Contact form has name, email, and message fields
  - Projects page lists at least 3 projects
  - All inter-page links are correct (href values match filenames)

## Constraints

- Plain HTML/CSS/JS — no frameworks, no build tools
- Tests use Python standard library only (no pip install needed)
- Must work by opening files directly in a browser

## Agent Assignments

- **frontend**: Build all 3 HTML pages with styling and navigation
- **qa**: Write test_site.py to validate the HTML structure and content

## Current Status

Not started.

## Decisions Log

| Date | Decision |
|------|----------|
BRIEF

# 6. Create arch.yaml — 2 agent types
cat > arch.yaml << YAML
project:
  name: "Portfolio Site"
  description: "Multi-page portfolio website with validation tests"
  repo: "."

archie:
  persona: "personas/archie.md"
  model: "claude-opus-4-6"

agent_pool:
  - id: frontend
    persona: "personas/frontend.md"
    model: "claude-sonnet-4-6"
    max_instances: 1

  - id: qa
    persona: "personas/qa.md"
    model: "claude-sonnet-4-6"
    max_instances: 1

settings:
  max_concurrent_agents: 3
  state_dir: "./state"
  mcp_port: 3999
  token_budget_usd: 10.00
YAML

# 7. Add .gitignore
cat > .gitignore << 'GI'
state/
.worktrees/
__pycache__/
GI

# 8. Initial commit
git add .
git commit -m "Initial project setup for UAT #6"

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
echo "  [ ] TWO agents get spawned (frontend + qa in agents panel)"
echo "  [ ] Both agents show 'working' status"
echo "  [ ] Activity log shows messages from both agents"
echo "  [ ] First agent finishes — gets merged and torn down"
echo "  [ ] Second agent finishes — gets merged and torn down"
echo "  [ ] close_project confirmation in dashboard"
echo ""
echo "AFTER SHUTDOWN, VERIFY:"
echo "  cd $UAT_DIR"
echo "  git log --oneline          # Should show 2+ merge commits"
echo "  git branch                  # Should be on main"
echo "  ls *.html                   # Should have index.html, projects.html, contact.html"
echo "  python3 test_site.py        # QA tests should pass"
echo "  open index.html             # Should work in browser"
echo ""
echo "Press Enter to launch..."
read

source "$ARCH_DIR/.venv/bin/activate"
python "$ARCH_DIR/arch.py" up
