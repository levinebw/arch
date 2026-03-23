#!/usr/bin/env python3
"""
ARCH CLI - Agent Runtime & Coordination Harness

Usage:
  archie up [--config arch.yaml] [--keep-worktrees] [--clean]
        Start ARCH and launch Archie (--clean wipes state for fresh session)

  archie down
        Gracefully shut down all agents and clean up

  archie status
        Show current state of a running ARCH session

  archie init [--name "My Project"] [--github owner/repo]
        Scaffold arch.yaml + personas/ + BRIEF.md in current directory.
        If --github is provided: creates labels and default milestones in the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ASCII art banner
BANNER = r"""
    _   ____   ____  _   _
   / \ |  _ \ / ___|| | | |
  / _ \| |_) | |    | |_| |
 / ___ \  _ <| |___ |  _  |
/_/   \_\_| \_\____|_| |_|

"""

# Default config file name
DEFAULT_CONFIG = "arch.yaml"

# PID file for tracking running instance
PID_FILE = "state/arch.pid"


def print_banner():
    """Print the ARCH banner."""
    print(BANNER)


def get_state_dir(config_path: Path) -> Path:
    """Get the state directory from config, resolved relative to the config file."""
    import yaml

    config_dir = config_path.resolve().parent
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
        raw = config.get("settings", {}).get("state_dir", "./state")
        return (config_dir / raw).resolve()
    return (config_dir / "state").resolve()


def write_pid_file(state_dir: Path) -> None:
    """Write current PID to file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_path = state_dir / "arch.pid"
    pid_path.write_text(str(os.getpid()))


def read_pid_file(state_dir: Path) -> Optional[int]:
    """Read PID from file. Returns None if no running instance."""
    pid_path = state_dir / "arch.pid"
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        # Check if process is actually running
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        return None


def remove_pid_file(state_dir: Path) -> None:
    """Remove PID file."""
    pid_path = state_dir / "arch.pid"
    if pid_path.exists():
        pid_path.unlink()


# ============================================================================
# archie up
# ============================================================================


async def cmd_up(args: argparse.Namespace) -> int:
    """Start ARCH and launch Archie."""
    import logging
    from arch.orchestrator import Orchestrator

    # Configure logging so orchestrator output is visible
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy uvicorn/httpcore logs
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    config_path = Path(args.config)

    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Run 'archie init' to create a new project.")
        return 1

    state_dir = get_state_dir(config_path)

    # Check if already running
    existing_pid = read_pid_file(state_dir)
    if existing_pid:
        print(f"Error: ARCH is already running (PID {existing_pid})")
        print("Use 'archie status' to check the current state or 'archie down' to stop.")
        return 1

    import yaml
    with open(config_path) as f:
        raw_config = yaml.safe_load(f)
    mcp_port = raw_config.get("settings", {}).get("mcp_port", 3999)

    # Clear state directory if --clean flag
    if args.clean and state_dir.exists():
        import shutil
        # Preserve events.jsonl as historical record
        events_backup = None
        events_path = state_dir / "events.jsonl"
        if events_path.exists():
            events_backup = events_path.read_text()
        shutil.rmtree(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        if events_backup:
            events_path.write_text(events_backup)
        print("State directory cleared (--clean)")

    print_banner()
    print(f"Starting ARCH with config: {config_path}")
    print(f"Dashboard: http://localhost:{mcp_port}/dashboard")
    print()

    # Write PID file
    write_pid_file(state_dir)

    orchestrator = Orchestrator(config_path, keep_worktrees=args.keep_worktrees)

    try:
        if not await orchestrator.startup():
            remove_pid_file(state_dir)
            return 1

        await orchestrator.run()
        return 0

    except KeyboardInterrupt:
        print("\nShutdown requested...")
        return 0

    finally:
        await orchestrator.shutdown()
        remove_pid_file(state_dir)


# ============================================================================
# archie down
# ============================================================================


def cmd_down(args: argparse.Namespace) -> int:
    """Gracefully shut down all agents and clean up."""
    config_path = Path(args.config)
    state_dir = get_state_dir(config_path)

    pid = read_pid_file(state_dir)
    if not pid:
        print("ARCH is not running.")
        return 0

    print(f"Sending shutdown signal to ARCH (PID {pid})...")

    try:
        os.kill(pid, signal.SIGTERM)
        print("Shutdown signal sent. ARCH will shut down gracefully.")
        return 0
    except OSError as e:
        print(f"Error sending signal: {e}")
        return 1


# ============================================================================
# archie send
# ============================================================================


def cmd_send(args: argparse.Namespace) -> int:
    """Send a message to Archie via the message bus."""
    from arch.state import StateStore

    config_path = Path(args.config)
    state_dir = get_state_dir(config_path)

    if not state_dir.exists():
        print("Error: ARCH state directory not found.")
        print("Run 'archie up' first to start ARCH.")
        return 1

    # Check if ARCH is running
    pid = read_pid_file(state_dir)
    if not pid:
        print("Warning: ARCH is not running. Message will be queued.")

    # Load state and add message
    state = StateStore(state_dir)
    message = state.add_message(
        from_agent="user",
        to_agent="archie",
        content=args.message
    )

    print(f"Message sent to Archie (id: {message['id']})")

    if pid:
        print("Archie will see this message on next get_messages call.")
    else:
        print("Start ARCH with 'archie up' - Archie will auto-resume to handle the message.")

    return 0


# ============================================================================
# archie dashboard
# ============================================================================


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the web dashboard in the default browser."""
    import urllib.request
    import webbrowser
    import yaml

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Run 'archie init' to create a new project.")
        return 1

    with open(config_path) as f:
        config = yaml.safe_load(f)

    mcp_port = config.get("settings", {}).get("mcp_port", 3999)
    url = f"http://localhost:{mcp_port}/dashboard"

    # Check if ARCH is running
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{mcp_port}/api/health", timeout=2)
    except Exception:
        print(f"Error: ARCH is not running on port {mcp_port}.")
        print("Start ARCH first with 'archie up'.")
        return 1

    print(f"Opening dashboard: {url}")
    webbrowser.open(url)
    return 0


# ============================================================================
# archie status
# ============================================================================


def cmd_status(args: argparse.Namespace) -> int:
    """Show current state of a running ARCH session."""
    config_path = Path(args.config)
    state_dir = get_state_dir(config_path)

    pid = read_pid_file(state_dir)

    print("ARCH Status")
    print("=" * 40)

    if pid:
        print(f"Status: Running (PID {pid})")
    else:
        print("Status: Not running")

    # Try to read state files
    agents_path = state_dir / "agents.json"
    project_path = state_dir / "project.json"

    if project_path.exists():
        with open(project_path) as f:
            project = json.load(f)
        print(f"Project: {project.get('name', 'Unknown')}")
        if project.get("started_at"):
            started = project["started_at"]
            print(f"Started: {started}")

    if agents_path.exists():
        with open(agents_path) as f:
            agents = json.load(f)

        print()
        print("Agents:")
        print("-" * 40)

        if not agents:
            print("  (none)")
        else:
            for agent_id, agent in agents.items():
                status = agent.get("status", "unknown")
                role = agent.get("role", "unknown")
                task = agent.get("task", "")
                sandboxed = "[c]" if agent.get("sandboxed") else ""
                skip_perms = "[!]" if agent.get("skip_permissions") else ""
                flags = f"{sandboxed}{skip_perms}"
                if flags:
                    flags = f" {flags}"
                print(f"  {agent_id}{flags}: {status}")
                if task:
                    print(f"    └─ {task[:50]}...")

    # Show token usage if available
    usage_path = state_dir / "usage.json"
    if usage_path.exists():
        with open(usage_path) as f:
            usage = json.load(f)

        print()
        print("Token Usage:")
        print("-" * 40)

        total_cost = 0.0
        for agent_id, data in usage.items():
            cost = data.get("cost_usd", 0.0)
            total_cost += cost
            print(f"  {agent_id}: ${cost:.4f}")

        print(f"  {'─' * 20}")
        print(f"  Total: ${total_cost:.4f}")

    return 0


# ============================================================================
# archie init
# ============================================================================


DEFAULT_ARCH_YAML = '''# ARCH Configuration
# See: https://github.com/levinebw/arch

project:
  name: "{project_name}"
  description: "{project_description}"
  repo: "."

archie:
  persona: "personas/archie.md"
  model: "claude-opus-4-5"

agent_pool:
  - id: frontend
    persona: "personas/frontend.md"
    model: "claude-sonnet-4-6"
    max_instances: 2

  - id: backend
    persona: "personas/backend.md"
    model: "claude-sonnet-4-6"
    max_instances: 2

  - id: qa
    persona: "personas/qa.md"
    model: "claude-sonnet-4-6"
    max_instances: 1

settings:
  max_concurrent_agents: 5
  state_dir: "./state"
  mcp_port: 3999
  # token_budget_usd: 10.00
  # auto_merge: false
'''

DEFAULT_BRIEF_MD = '''# {project_name}

## Goal

<!-- What does this project achieve? One or two sentences. -->

## This Session

<!-- What should Archie focus on RIGHT NOW? Be specific about tasks, agent roles, and scope.
     Example: "Focus on TASK-003 only. Spawn one backend-dev agent." -->

## Done When (this session)

<!-- Concrete, testable criteria scoped to THIS session's work.
     Archie checks these off as work completes. -->
- [ ]

## Done When (project)

<!-- Overall project completion criteria. Archie uses these for context
     but focuses on the session criteria above. -->
- [ ]

## Completed

<!-- Checked-off items from previous sessions. Helps Archie understand
     what's already done and avoid re-doing work. -->

## Backlog

<!-- Tasks not in scope for this session. Listed for context only.
     Archie should NOT start these unless explicitly told to. -->

## Constraints

<!-- Technical requirements, security rules, scope boundaries. -->

## Current Status

<!-- Updated by Archie throughout the session. -->
Not started.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
'''

DEFAULT_GITIGNORE_ADDITIONS = '''
# ARCH
state/
.worktrees/
'''


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold arch.yaml + personas/ + BRIEF.md in current directory."""
    project_name = args.name or "My Project"
    project_description = args.description or "A new ARCH project"
    github_repo = args.github

    print(f"Initializing ARCH project: {project_name}")
    print()

    # Create directories
    personas_dir = Path("personas")
    state_dir = Path("state")

    personas_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    # Create arch.yaml
    arch_yaml = Path("arch.yaml")
    if arch_yaml.exists():
        print(f"  ⚠  arch.yaml already exists, skipping")
    else:
        content = DEFAULT_ARCH_YAML.format(
            project_name=project_name,
            project_description=project_description,
        )
        if github_repo:
            content += f'''
github:
  repo: "{github_repo}"
  default_branch: "main"
  labels:
    - name: "agent:archie"
      color: "7057ff"
    - name: "agent:frontend"
      color: "0075ca"
    - name: "agent:backend"
      color: "e99695"
    - name: "agent:qa"
      color: "008672"
'''
        arch_yaml.write_text(content)
        print(f"  ✓  Created arch.yaml")

    # Create BRIEF.md
    brief_md = Path("BRIEF.md")
    if brief_md.exists():
        print(f"  ⚠  BRIEF.md already exists, skipping")
    else:
        brief_md.write_text(DEFAULT_BRIEF_MD.format(project_name=project_name))
        print(f"  ✓  Created BRIEF.md")

    # Copy persona files if they don't exist
    personas_to_copy = ["archie", "frontend", "backend", "qa", "security", "copywriter"]
    source_personas = Path(__file__).parent / "personas"

    for persona in personas_to_copy:
        target = personas_dir / f"{persona}.md"
        source = source_personas / f"{persona}.md"

        if target.exists():
            print(f"  ⚠  personas/{persona}.md already exists, skipping")
        elif source.exists():
            target.write_text(source.read_text())
            print(f"  ✓  Created personas/{persona}.md")
        else:
            # Create minimal persona if source doesn't exist
            target.write_text(f"# {persona.title()}\n\nYou are a {persona} agent.\n")
            print(f"  ✓  Created personas/{persona}.md (minimal)")

    # Update .gitignore
    gitignore = Path(".gitignore")
    if gitignore.exists():
        content = gitignore.read_text()
        if "state/" not in content:
            with open(gitignore, "a") as f:
                f.write(DEFAULT_GITIGNORE_ADDITIONS)
            print(f"  ✓  Updated .gitignore")
        else:
            print(f"  ⚠  .gitignore already has ARCH entries")
    else:
        gitignore.write_text(DEFAULT_GITIGNORE_ADDITIONS.strip() + "\n")
        print(f"  ✓  Created .gitignore")

    # GitHub setup
    if github_repo:
        print()
        print("Setting up GitHub repository...")
        setup_github(github_repo)

    print()
    print("Done! Next steps:")
    print("  1. Edit BRIEF.md to describe your project goals")
    print("  2. Review arch.yaml configuration")
    print("  3. Run 'archie up' to start")

    return 0


def setup_github(repo: str) -> None:
    """Create labels and milestone in GitHub repo."""
    labels = [
        ("agent:archie", "7057ff", "Work by Archie (lead agent)"),
        ("agent:frontend", "0075ca", "Work by frontend agent"),
        ("agent:backend", "e99695", "Work by backend agent"),
        ("agent:qa", "008672", "Work by QA agent"),
        ("phase:0", "c5def5", "Initial phase"),
        ("phase:1", "bfd4f2", "Phase 1"),
        ("blocked", "d93f0b", "Blocked on external dependency"),
    ]

    print(f"  Creating labels in {repo}...")

    for name, color, description in labels:
        cmd = [
            "gh", "label", "create", name,
            "--repo", repo,
            "--color", color,
            "--description", description,
            "--force",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(f"    ✓  Label: {name}")
            else:
                print(f"    ⚠  Label {name}: {result.stderr.strip()}")
        except FileNotFoundError:
            print("    ✗  gh CLI not found. Install from https://cli.github.com/")
            return
        except subprocess.TimeoutExpired:
            print(f"    ✗  Timeout creating label {name}")

    # Create initial milestone
    print(f"  Creating initial milestone...")
    due_date = (datetime.now(timezone.utc).replace(day=1) +
                timedelta(days=32)).strftime("%Y-%m-%d")

    cmd = [
        "gh", "api", f"repos/{repo}/milestones",
        "-X", "POST",
        "-f", "title=Sprint 1",
        "-f", "description=Initial sprint",
        "-f", f"due_on={due_date}T00:00:00Z",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"    ✓  Milestone: Sprint 1")
        else:
            # Might already exist
            if "already_exists" in result.stderr.lower():
                print(f"    ⚠  Milestone Sprint 1 already exists")
            else:
                print(f"    ⚠  Milestone: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print(f"    ✗  Timeout creating milestone")


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="archie",
        description="ARCH - Agent Runtime & Coordination Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  up         Start ARCH orchestrator (run in one terminal)
  dashboard  Open the web dashboard in your browser
  down       Gracefully shut down all agents
  status     Show current state of running session
  send       Send a message to Archie
  init       Scaffold a new ARCH project

Examples:
  archie init --name "My App" --github myorg/myapp
  archie up                                    # Terminal 1
  archie dashboard                             # Terminal 2
  archie send "Please review the test results"
  archie status
  archie down
"""
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # archie up
    up_parser = subparsers.add_parser("up", help="Start ARCH and launch Archie")
    up_parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )
    up_parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Don't remove worktrees on shutdown"
    )
    up_parser.add_argument(
        "--clean",
        action="store_true",
        help="Clear state directory before starting (fresh session)"
    )

    # archie down
    down_parser = subparsers.add_parser("down", help="Gracefully shut down")
    down_parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )

    # archie send
    send_parser = subparsers.add_parser("send", help="Send a message to Archie")
    send_parser.add_argument(
        "message",
        help="Message to send to Archie"
    )
    send_parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )

    # archie dashboard
    dash_parser = subparsers.add_parser("dashboard", help="Open the web dashboard")
    dash_parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )

    # archie status
    status_parser = subparsers.add_parser("status", help="Show current state")
    status_parser.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG,
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )

    # archie init
    init_parser = subparsers.add_parser("init", help="Scaffold a new project")
    init_parser.add_argument(
        "--name", "-n",
        help="Project name"
    )
    init_parser.add_argument(
        "--description", "-d",
        help="Project description"
    )
    init_parser.add_argument(
        "--github", "-g",
        metavar="OWNER/REPO",
        help="GitHub repo to configure (creates labels/milestones)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "up":
        return asyncio.run(cmd_up(args))
    elif args.command == "down":
        return cmd_down(args)
    elif args.command == "send":
        return cmd_send(args)
    elif args.command == "dashboard":
        return cmd_dashboard(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "init":
        return cmd_init(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
