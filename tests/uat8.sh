#!/bin/bash
#
# UAT #8 Setup & Launch Script
# Creates arch-test-8 project and starts ARCH
#
# WHAT THIS TESTS (full lifecycle with iteration):
#   1. No agent_pool — Archie plans team from 5 personas (should pick 4, skip security)
#   2. 4 worker agents in parallel (backend, frontend, qa, copywriter)
#   3. Sequential dependencies — CLI needs core lib, QA needs both, docs needs all
#   4. Agents run tests directly (python -m pytest) — no user escalation
#   5. QA iteration — finds bugs, reports to Archie, dev fixes
#   6. Done When items checked off progressively after each merge
#   7. BRIEF.md current_status updated throughout session
#   8. Close project with user confirmation
#   9. Custom feedback at close → Archie keeps working
#
set -e

UAT_DIR="$HOME/claude-projects/arch-test-8"
ARCH_DIR="$HOME/claude-projects/arch"

echo "=== ARCH UAT #8 Setup (Full Lifecycle with Iteration) ==="
echo ""

# 1. Clean up any previous run
if [ -d "$UAT_DIR" ]; then
    echo "Removing previous arch-test-8..."
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

# 4. Create personas (Archie + 5 specialists — Archie should pick 4)
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
- Dependencies on other agents' work

When agents have dependencies on each other's work, note this in the assignment.
The agent can check `git log` to see if dependent work has been merged yet.

## Monitoring

- Call `list_agents` to check agent status
- Call `get_messages` to read agent messages
- Actively monitor for blocked agents and help unblock them

## Completing Work

When an agent calls `report_completion`:
1. Review their summary and artifacts
2. Check against the BRIEF.md "Done When" criteria
3. If incomplete, message them with specific feedback
4. If complete, proceed to merge
5. **After each merge**, check off completed Done When items:
   ```
   update_brief(section: "done_when", content: "dataforge/core.py")
   ```
6. Update current status after significant progress:
   ```
   update_brief(section: "current_status", content: "Core library complete. CLI and tests in progress.")
   ```

Wait for ALL agents to complete before closing.

## Handling QA Failures

When QA reports test failures:
1. Review the failure details in QA's message
2. Determine which agent's code has the bug
3. Message that agent with specific fix instructions
4. Do NOT tear down QA until all tests pass

## Session Shutdown

When all "Done When" criteria are met:
1. Verify all Done When items are checked off in BRIEF.md
2. Call `close_project(summary: "...")` with a complete summary

## IMPORTANT
- You are the coordinator, NOT the implementer
- ALWAYS call list_personas and plan_team before spawning any agents
- Only spawn roles that are actually needed for this project
- Always spawn agents to do the actual coding
- Merge each agent's work as it completes
- Always tear down agents when their work is merged
- Check off Done When items after EVERY merge
- Keep current_status updated throughout the session
PERSONA

cat > personas/frontend.md << 'PERSONA'
# Frontend / CLI Developer

You build user-facing interfaces — web UIs, CLIs, and interactive tools.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Commit your work to git frequently
- If you depend on another agent's work, check `git log` to see if it's been merged
- If blocked, set status to "blocked" and message Archie

## When Complete
1. Run any relevant tests to verify your work
2. Make sure all files are committed to git
3. Call `report_completion(summary: "what you built", artifacts: ["file1.py"])`
PERSONA

cat > personas/backend.md << 'PERSONA'
# Backend / Core Developer

You build core libraries, APIs, data models, and business logic.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Commit your work to git frequently
- Write clean, well-documented functions with docstrings
- If blocked, set status to "blocked" and message Archie

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you built", artifacts: ["file1.py"])`
PERSONA

cat > personas/qa.md << 'PERSONA'
# QA Engineer

You write tests and validate that applications meet their requirements.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Write clear, runnable test scripts using pytest
- Commit your work to git frequently
- **Run tests yourself** using `python -m pytest` to verify they pass
- If tests fail due to bugs in other agents' code, message Archie with details

## When Complete
1. Run the full test suite: `python -m pytest -v`
2. Make sure all files are committed to git
3. Call `report_completion(summary: "what you tested and results", artifacts: ["test_file.py"])`
PERSONA

cat > personas/copywriter.md << 'PERSONA'
# Technical Writer / Copywriter

You write documentation, READMEs, guides, and technical content.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Read the source code to understand the API before documenting
- Include practical usage examples
- Commit your work to git frequently

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "what you wrote", artifacts: ["README.md"])`
PERSONA

cat > personas/security.md << 'PERSONA'
# Security Engineer

You review code for security vulnerabilities, perform threat modeling, and ensure
applications follow security best practices.

## When You Start
1. Read your assignment carefully
2. Call `update_status(status: "working", task: "your current task")`

## While Working
- Keep Archie informed via `send_message(to: "archie", content: "status update")`
- Review code for OWASP Top 10 vulnerabilities
- Check for injection, path traversal, and unsafe deserialization

## When Complete
1. Make sure all files are committed to git
2. Call `report_completion(summary: "security findings", artifacts: ["SECURITY.md"])`
PERSONA

# 5. Create BRIEF.md
cat > BRIEF.md << 'BRIEF'
# DataForge — Python Data Transformation CLI

## Goals

Build a Python command-line tool for common data transformations. DataForge reads
CSV or JSON files, applies transformations, and writes the result to a new file.
All using Python standard library only (no pip installs).

## Done When

- [ ] `dataforge/core.py` — Core library with at least 3 transformation functions:
  - `csv_to_json(input_path, output_path)` — Convert CSV to JSON
  - `json_to_csv(input_path, output_path)` — Convert JSON to CSV
  - `filter_rows(input_path, output_path, column, value)` — Filter rows where column matches value
  - `sort_rows(input_path, output_path, column, reverse=False)` — Sort rows by column
- [ ] `dataforge/__init__.py` — Package init that exports core functions
- [ ] `dataforge/cli.py` — CLI using argparse with subcommands:
  - `dataforge csv2json input.csv output.json`
  - `dataforge json2csv input.json output.csv`
  - `dataforge filter input.csv output.csv --column name --value Alice`
  - `dataforge sort input.csv output.csv --column age [--reverse]`
- [ ] `test_core.py` — Unit tests for all core functions (must pass)
- [ ] `test_cli.py` — CLI integration tests using subprocess (must pass)
- [ ] `README.md` — Documentation with:
  - Project description
  - Installation (just clone, no pip needed)
  - Usage examples for each subcommand
  - Example input/output data
- [ ] All tests pass: `python -m pytest test_core.py test_cli.py -v`

## Constraints

- Python standard library only — no third-party packages
- Use `csv`, `json`, `argparse` modules
- Tests use `pytest` (assume it's available) and `subprocess` for CLI tests
- All functions should handle errors gracefully (file not found, invalid format)
- CLI should use `python -m dataforge` entry point

## Current Status

Not started.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
BRIEF

# 6. Create arch.yaml — NO agent_pool (forces dynamic team planning)
cat > arch.yaml << YAML
project:
  name: "DataForge CLI"
  description: "Python data transformation CLI tool"
  repo: "."

archie:
  persona: "personas/archie.md"
  model: "claude-opus-4-6"

# No agent_pool — Archie must call list_personas and plan_team
# to propose the team dynamically based on BRIEF.md
# 5 personas available: backend, frontend, qa, copywriter, security
# Archie should pick ~4 (skip security — not needed for a CLI tool)

settings:
  max_concurrent_agents: 5
  state_dir: "./state"
  mcp_port: 3999
  token_budget_usd: 15.00
  auto_approve_team: false
YAML

# 7. Create the dataforge package directory
mkdir -p dataforge

# 8. Create __main__.py so `python -m dataforge` works
cat > dataforge/__main__.py << 'MAIN'
"""Entry point for python -m dataforge."""
from dataforge.cli import main

if __name__ == "__main__":
    main()
MAIN

# 9. Create sample test data for agents to use
mkdir -p testdata

cat > testdata/sample.csv << 'CSV'
name,age,city
Alice,30,New York
Bob,25,San Francisco
Charlie,35,Chicago
Diana,28,New York
Eve,32,San Francisco
CSV

cat > testdata/sample.json << 'JSON'
[
  {"name": "Alice", "age": 30, "city": "New York"},
  {"name": "Bob", "age": 25, "city": "San Francisco"},
  {"name": "Charlie", "age": 35, "city": "Chicago"},
  {"name": "Diana", "age": 28, "city": "New York"},
  {"name": "Eve", "age": 32, "city": "San Francisco"}
]
JSON

# 10. Add .gitignore
cat > .gitignore << 'GI'
state/
.worktrees/
__pycache__/
*.pyc
testdata/output_*
GI

# 11. Add pytest config
cat > pytest.ini << 'PYTEST'
[pytest]
testpaths = .
python_files = test_*.py
PYTEST

# 12. Initial commit
git add .
git commit -m "Initial project setup for UAT #8 — DataForge CLI"

echo ""
echo "=== Project created at $UAT_DIR ==="
echo ""
echo "Contents:"
ls -la
echo ""
echo "Available personas:"
ls personas/
echo ""
echo "Package structure:"
find . -name "*.py" -o -name "*.csv" -o -name "*.json" | grep -v state | grep -v .git | sort
echo ""
echo "arch.yaml (note: NO agent_pool):"
cat arch.yaml
echo ""
echo "=== Launching ARCH ==="
echo ""
echo "CHECKLIST (watch for these in the dashboard):"
echo ""
echo "  PHASE 1 — Team Planning:"
echo "  [ ] Archie calls list_personas (discovers 5 personas)"
echo "  [ ] Archie calls plan_team (proposes ~4 agents, skips security)"
echo "  [ ] ESCALATION: Team plan with approve/reject buttons"
echo "  [ ] Approve the team plan"
echo ""
echo "  PHASE 2 — Parallel Development:"
echo "  [ ] 4 agents spawned (backend, frontend, qa, copywriter)"
echo "  [ ] All agents show 'working' status"
echo "  [ ] Activity log shows inter-agent messages"
echo "  [ ] Backend agent builds core.py and __init__.py"
echo "  [ ] Frontend agent builds cli.py"
echo "  [ ] QA agent writes test_core.py and test_cli.py"
echo "  [ ] Copywriter agent writes README.md"
echo ""
echo "  PHASE 3 — Testing & Iteration:"
echo "  [ ] QA runs 'python -m pytest -v' directly (no user escalation)"
echo "  [ ] If tests fail, QA messages Archie with details"
echo "  [ ] Archie forwards bug report to responsible agent"
echo "  [ ] Agent fixes and re-reports completion"
echo ""
echo "  PHASE 4 — Merging & BRIEF Updates:"
echo "  [ ] Each merge triggers done_when checkbox updates"
echo "  [ ] current_status updated after each major milestone"
echo "  [ ] BRIEF.md shows progressive completion"
echo ""
echo "  PHASE 5 — Closeout:"
echo "  [ ] ESCALATION: 'Is everything done?' with buttons"
echo "  [ ] (Optional) Give custom feedback to test iteration"
echo "  [ ] Confirm shutdown"
echo "  [ ] Dashboard shows COMPLETE"
echo ""
echo "AFTER SHUTDOWN, VERIFY:"
echo "  cd $UAT_DIR"
echo "  git log --oneline              # Should show 4+ merge commits"
echo "  cat BRIEF.md                   # Done When items should be [x] checked"
echo "  python -m pytest -v            # All tests should pass"
echo "  python -m dataforge csv2json testdata/sample.csv /tmp/out.json"
echo "  python -m dataforge filter testdata/sample.csv /tmp/out.csv --column city --value 'New York'"
echo "  cat state/events.jsonl | grep plan_team"
echo ""
echo "Dashboard opens automatically at http://localhost:3999/dashboard"
echo "Or run: python $ARCH_DIR/arch.py dashboard --config $UAT_DIR/arch.yaml"
echo ""
echo "Press Enter to launch orchestrator..."
read

source "$ARCH_DIR/.venv/bin/activate"
python "$ARCH_DIR/arch.py" up
