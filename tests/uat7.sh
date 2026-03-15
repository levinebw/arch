#!/bin/bash
#
# UAT #7 Setup & Launch Script
# Creates arch-test-7 project and starts ARCH
#
# WHAT THIS TESTS (dynamic team planning):
#   1. No agent_pool in arch.yaml — Archie must plan the team
#   2. Archie reads BRIEF.md and calls list_personas
#   3. Archie calls plan_team with proposed roles
#   4. User approves the team plan via dashboard escalation
#   5. Archie spawns the approved agents
#   6. Agents build a landing page + tests in parallel
#   7. Agents report completion, Archie merges and tears down
#   8. Archie calls close_project — user confirms "done" via escalation
#   9. Graceful shutdown
#
set -e

UAT_DIR="$HOME/claude-projects/arch-test-7"
ARCH_DIR="$HOME/claude-projects/arch"

echo "=== ARCH UAT #7 Setup (Dynamic Team Planning) ==="
echo ""

# 1. Clean up any previous run
if [ -d "$UAT_DIR" ]; then
    echo "Removing previous arch-test-7..."
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

# 4. Create personas (Archie + 3 available specialists)
#    Archie should pick frontend + qa based on the BRIEF.
#    The backend persona exists but isn't needed — tests that Archie
#    doesn't blindly spawn every persona.
mkdir -p personas

cat > personas/archie.md << 'PERSONA'
# Archie — Lead Agent

You are **Archie**, the Lead Agent for ARCH.

## Session Startup

1. Call `get_project_context` as your **first action**.
2. Read the BRIEF.md goals and "Done When" criteria carefully.
3. Plan the team:
   a. Call `list_personas` to see what agent personas are available.
   b. Analyze which roles are needed based on the project scope.
   c. Call `plan_team` with your proposed team and rationale.
   d. Wait for user approval before spawning anyone.
4. After approval, use `spawn_agent` for each approved role.

## Spawning Agents

Be specific about assignments. Include:
- What to build or test
- Acceptance criteria from BRIEF.md
- File paths and constraints

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
1. Call `close_project(summary: "...")` with a summary of what was accomplished

## IMPORTANT
- You are the coordinator, NOT the implementer
- ALWAYS call list_personas and plan_team before spawning any agents
- Only spawn roles that are actually needed for this project
- Always spawn agents to do the actual coding
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

cat > personas/backend.md << 'PERSONA'
# Backend Developer

You build server-side applications, APIs, and databases.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Commit your work to git frequently

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you built", artifacts: ["app.py"])`
PERSONA

# 5. Create BRIEF.md — landing page (frontend + QA, no backend needed)
cat > BRIEF.md << 'BRIEF'
# Product Landing Page

## Goals

Build a product landing page for "CloudSync" — a fictional file sync service.

## Done When

- [ ] index.html — Landing page with:
  - Hero section with product name, tagline, and CTA button
  - Features section listing 3 key features with icons (emoji ok)
  - Pricing section with 3 tiers (Free, Pro, Enterprise) in card layout
  - Footer with copyright and links
- [ ] Clean, modern CSS styling (dark or light theme)
- [ ] Mobile responsive (works on phone screens)
- [ ] test_landing.py — Python test script that validates:
  - index.html exists and is valid HTML
  - Contains hero section with h1 and CTA link/button
  - Contains exactly 3 feature items
  - Contains exactly 3 pricing cards/tiers
  - Contains a footer element
  - Page is under 50KB total size

## Constraints

- Single HTML file with embedded CSS — no frameworks, no build tools
- Tests use Python standard library only (no pip install needed)
- Must work by opening the file directly in a browser

## Current Status

Not started.

## Decisions Log

| Date | Decision |
|------|----------|
BRIEF

# 6. Create arch.yaml — NO agent_pool (forces dynamic team planning)
cat > arch.yaml << YAML
project:
  name: "CloudSync Landing Page"
  description: "Product landing page for a fictional file sync service"
  repo: "."

archie:
  persona: "personas/archie.md"
  model: "claude-opus-4-6"

# No agent_pool — Archie must call list_personas and plan_team
# to propose the team dynamically based on BRIEF.md

settings:
  max_concurrent_agents: 3
  state_dir: "./state"
  mcp_port: 3999
  token_budget_usd: 10.00
  auto_approve_team: false
YAML

# 7. Add .gitignore
cat > .gitignore << 'GI'
state/
.worktrees/
__pycache__/
GI

# 8. Initial commit
git add .
git commit -m "Initial project setup for UAT #7"

echo ""
echo "=== Project created at $UAT_DIR ==="
echo ""
echo "Contents:"
ls -la
echo ""
echo "Available personas:"
ls personas/
echo ""
echo "BRIEF.md:"
cat BRIEF.md
echo ""
echo "arch.yaml (note: NO agent_pool):"
cat arch.yaml
echo ""
echo "=== Launching ARCH ==="
echo ""
echo "CHECKLIST (watch for these in the dashboard):"
echo "  [ ] Dashboard appears with ARCH title"
echo "  [ ] Archie appears in agents panel"
echo "  [ ] Archie calls list_personas (check event log: e)"
echo "  [ ] Archie calls plan_team (check event log: e)"
echo "  [ ] ESCALATION: Team plan appears with approve/reject buttons"
echo "       - Verify Archie proposes frontend + qa (NOT backend)"
echo "       - Approve the team plan"
echo "  [ ] Two agents get spawned after approval"
echo "  [ ] Both agents show 'working' status"
echo "  [ ] Activity log shows messages from agents"
echo "  [ ] Agents complete and get merged/torn down"
echo "  [ ] ESCALATION: 'Is everything done?' with confirm/reject buttons"
echo "       - Verify the summary looks correct"
echo "       - Confirm shutdown"
echo "  [ ] Dashboard shows COMPLETE"
echo "  [ ] Press q to quit dashboard"
echo ""
echo "AFTER SHUTDOWN, VERIFY:"
echo "  cd $UAT_DIR"
echo "  git log --oneline          # Should show merge commits"
echo "  ls *.html                  # Should have index.html"
echo "  python3 test_landing.py    # QA tests should pass"
echo "  open index.html            # Should look like a real landing page"
echo "  cat state/events.jsonl | grep plan_team  # Should see the plan_team call"
echo ""
echo "WHAT THIS TESTS BEYOND UAT #6:"
echo "  - Dynamic team planning (no pre-configured agent_pool)"
echo "  - Archie correctly identifies needed roles from BRIEF.md"
echo "  - Archie does NOT spawn unneeded roles (backend)"
echo "  - User approval gate for team plan"
echo "  - User confirmation gate before shutdown"
echo "  - Escalation buttons in dashboard"
echo ""
echo "Start dashboard in another terminal:"
echo "  source $ARCH_DIR/.venv/bin/activate"
echo "  python $ARCH_DIR/arch.py dashboard --config $UAT_DIR/arch.yaml"
echo ""
echo "Press Enter to launch orchestrator..."
read

source "$ARCH_DIR/.venv/bin/activate"
python "$ARCH_DIR/arch.py" up
