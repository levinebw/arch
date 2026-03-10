"""
ARCH Orchestrator

Responsible for the full lifecycle of the ARCH system:
- Parse and validate arch.yaml
- Initialize all components (state, worktree, MCP server, sessions)
- Startup/shutdown sequences
- Signal handlers for graceful cleanup
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from arch.container import check_docker_available, check_image_exists, pull_image
from arch.mcp_server import MCPServer
from arch.session import AgentConfig, SessionManager, AnySession
from arch.state import StateStore
from arch.token_tracker import TokenTracker
from arch.worktree import WorktreeManager

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULT_MCP_PORT = 3999
DEFAULT_STATE_DIR = "./state"
DEFAULT_MAX_CONCURRENT_AGENTS = 5
DEFAULT_ARCHIE_MODEL = "claude-opus-4-6"
DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"
DEFAULT_CONTAINER_IMAGE = "arch-agent:latest"
DEFAULT_ARCHIE_PERSONA = "personas/archie.md"
DEFAULT_SHUTDOWN_TIMEOUT = 30

# Default allowed tools for all agents
# These map to --allowedTools CLI flags
DEFAULT_ALLOWED_TOOLS_ALL = [
    # File operations (also covered by --permission-mode acceptEdits, belt-and-suspenders)
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    # Git operations
    "Bash(git status)",
    "Bash(git diff *)",
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git log *)",
    "Bash(git branch *)",
    "Bash(git checkout *)",
    # Dev tools — agents need to run tests, builds, and scripts
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(node *)",
    "Bash(npm *)",
    "Bash(npx *)",
    "Bash(pip *)",
    "Bash(cat *)",
    "Bash(ls *)",
    "Bash(mkdir *)",
    "Bash(cp *)",
    "Bash(mv *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(wc *)",
    "Bash(find *)",
    "Bash(grep *)",
    "Bash(sort *)",
    "Bash(cd *)",
    # MCP tools available to all agents
    "mcp__arch__send_message",
    "mcp__arch__get_messages",
    "mcp__arch__update_status",
    "mcp__arch__save_progress",
    "mcp__arch__report_completion",
]

# Additional tools for Archie (lead agent coordination)
DEFAULT_ALLOWED_TOOLS_ARCHIE = [
    *DEFAULT_ALLOWED_TOOLS_ALL,
    "Bash(gh *)",  # GitHub CLI for issue/PR management
    # Archie-only MCP tools
    "mcp__arch__spawn_agent",
    "mcp__arch__teardown_agent",
    "mcp__arch__list_agents",
    "mcp__arch__escalate_to_user",
    "mcp__arch__request_merge",
    "mcp__arch__get_project_context",
    "mcp__arch__close_project",
    "mcp__arch__update_brief",
    "mcp__arch__list_personas",
    "mcp__arch__plan_team",
    "mcp__arch__gh_create_issue",
    "mcp__arch__gh_list_issues",
    "mcp__arch__gh_close_issue",
    "mcp__arch__gh_update_issue",
    "mcp__arch__gh_add_comment",
    "mcp__arch__gh_create_milestone",
    "mcp__arch__gh_list_milestones",
]


# ============================================================================
# Configuration Dataclasses
# ============================================================================


@dataclass
class ProjectConfig:
    """Project configuration from arch.yaml."""
    name: str
    description: str = ""
    repo: str = "."


@dataclass
class ArchieConfig:
    """Archie (lead agent) configuration."""
    persona: str = DEFAULT_ARCHIE_PERSONA
    model: str = DEFAULT_ARCHIE_MODEL


@dataclass
class SandboxConfig:
    """Container sandbox settings for an agent."""
    enabled: bool = False
    image: str = DEFAULT_CONTAINER_IMAGE
    extra_mounts: list[str] = field(default_factory=list)
    network: str = "bridge"
    memory_limit: Optional[str] = None
    cpus: Optional[float] = None


@dataclass
class PermissionsConfig:
    """Permission settings for an agent."""
    skip_permissions: bool = False
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class AgentPoolEntry:
    """Configuration for an agent type in the pool."""
    id: str
    persona: str
    model: str = DEFAULT_AGENT_MODEL
    max_instances: int = 1
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)


@dataclass
class GitHubLabel:
    """GitHub label configuration."""
    name: str
    color: str


@dataclass
class GitHubConfig:
    """GitHub integration configuration."""
    repo: str
    default_branch: str = "main"
    labels: list[GitHubLabel] = field(default_factory=list)
    issue_template: Optional[str] = None


@dataclass
class SettingsConfig:
    """General settings."""
    max_concurrent_agents: int = DEFAULT_MAX_CONCURRENT_AGENTS
    state_dir: str = DEFAULT_STATE_DIR
    mcp_port: int = DEFAULT_MCP_PORT
    token_budget_usd: Optional[float] = None
    auto_merge: bool = False
    auto_approve_team: bool = False
    require_user_approval: list[str] = field(default_factory=list)


@dataclass
class ArchConfig:
    """Complete ARCH configuration from arch.yaml."""
    project: ProjectConfig
    archie: ArchieConfig = field(default_factory=ArchieConfig)
    agent_pool: list[AgentPoolEntry] = field(default_factory=list)
    github: Optional[GitHubConfig] = None
    settings: SettingsConfig = field(default_factory=SettingsConfig)


def parse_config(config_path: Path) -> ArchConfig:
    """
    Parse arch.yaml into typed configuration.

    Args:
        config_path: Path to arch.yaml file.

    Returns:
        Parsed ArchConfig.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        ValueError: If config is invalid.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError("Config file is empty")

    if "project" not in raw:
        raise ValueError("Config must have 'project' section")

    if "name" not in raw["project"]:
        raise ValueError("Config project.name is required")

    # Parse project
    project = ProjectConfig(
        name=raw["project"]["name"],
        description=raw["project"].get("description", ""),
        repo=raw["project"].get("repo", "."),
    )

    # Parse archie
    archie_raw = raw.get("archie", {})
    archie = ArchieConfig(
        persona=archie_raw.get("persona", DEFAULT_ARCHIE_PERSONA),
        model=archie_raw.get("model", DEFAULT_ARCHIE_MODEL),
    )

    # Parse agent_pool
    agent_pool = []
    for entry in raw.get("agent_pool", []):
        if "id" not in entry:
            raise ValueError("Each agent_pool entry must have 'id'")
        if "persona" not in entry:
            raise ValueError(f"Agent {entry['id']} must have 'persona'")

        sandbox_raw = entry.get("sandbox", {})
        sandbox = SandboxConfig(
            enabled=sandbox_raw.get("enabled", False),
            image=sandbox_raw.get("image", DEFAULT_CONTAINER_IMAGE),
            extra_mounts=sandbox_raw.get("extra_mounts", []),
            network=sandbox_raw.get("network", "bridge"),
            memory_limit=sandbox_raw.get("memory_limit"),
            cpus=sandbox_raw.get("cpus"),
        )

        perms_raw = entry.get("permissions", {})
        permissions = PermissionsConfig(
            skip_permissions=perms_raw.get("skip_permissions", False),
            allowed_tools=perms_raw.get("allowed_tools", []),
        )

        agent_pool.append(AgentPoolEntry(
            id=entry["id"],
            persona=entry["persona"],
            model=entry.get("model", DEFAULT_AGENT_MODEL),
            max_instances=entry.get("max_instances", 1),
            sandbox=sandbox,
            permissions=permissions,
        ))

    # Parse github
    github = None
    if "github" in raw and raw["github"]:
        gh_raw = raw["github"]
        if "repo" not in gh_raw:
            raise ValueError("github.repo is required if github section is present")

        labels = []
        for label in gh_raw.get("labels", []):
            labels.append(GitHubLabel(
                name=label["name"],
                color=label.get("color", "000000"),
            ))

        github = GitHubConfig(
            repo=gh_raw["repo"],
            default_branch=gh_raw.get("default_branch", "main"),
            labels=labels,
            issue_template=gh_raw.get("issue_template"),
        )

    # Parse settings
    settings_raw = raw.get("settings", {})
    settings = SettingsConfig(
        max_concurrent_agents=settings_raw.get("max_concurrent_agents", DEFAULT_MAX_CONCURRENT_AGENTS),
        state_dir=settings_raw.get("state_dir", DEFAULT_STATE_DIR),
        mcp_port=settings_raw.get("mcp_port", DEFAULT_MCP_PORT),
        token_budget_usd=settings_raw.get("token_budget_usd"),
        auto_merge=settings_raw.get("auto_merge", False),
        auto_approve_team=settings_raw.get("auto_approve_team", False),
        require_user_approval=settings_raw.get("require_user_approval", []),
    )

    return ArchConfig(
        project=project,
        archie=archie,
        agent_pool=agent_pool,
        github=github,
        settings=settings,
    )


# ============================================================================
# Gate Checks
# ============================================================================


def check_permission_gate(config: ArchConfig) -> list[str]:
    """
    Check if any agents require skip_permissions.

    Returns:
        List of agent IDs that have skip_permissions=True.
    """
    return [
        agent.id for agent in config.agent_pool
        if agent.permissions.skip_permissions
    ]


def check_container_gate(config: ArchConfig) -> tuple[bool, list[str], list[str]]:
    """
    Check if Docker is available and required images exist.

    Returns:
        Tuple of (docker_available, agents_needing_containers, missing_images).
    """
    sandboxed_agents = [
        agent for agent in config.agent_pool
        if agent.sandbox.enabled
    ]

    if not sandboxed_agents:
        return True, [], []

    # Check Docker availability
    available, msg = check_docker_available()
    if not available:
        return False, [a.id for a in sandboxed_agents], []

    # Check images
    images_needed = set(a.sandbox.image for a in sandboxed_agents)
    missing_images = [img for img in images_needed if not check_image_exists(img)]

    return True, [a.id for a in sandboxed_agents], missing_images


def check_github_gate(config: ArchConfig) -> tuple[bool, str]:
    """
    Check GitHub CLI availability and authentication.

    Returns:
        Tuple of (available, message).
    """
    if not config.github:
        return True, "GitHub integration not configured"

    try:
        # Check gh is installed
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, "gh CLI not found"

        # Check auth status
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"gh not authenticated: {result.stderr}"

        # Check repo access
        result = subprocess.run(
            ["gh", "repo", "view", config.github.repo],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return False, f"Cannot access repo {config.github.repo}: {result.stderr}"

        return True, f"GitHub access verified for {config.github.repo}"

    except FileNotFoundError:
        return False, "gh CLI not installed"
    except subprocess.TimeoutExpired:
        return False, "gh command timed out"
    except Exception as e:
        return False, f"GitHub check failed: {e}"


# ============================================================================
# Orchestrator
# ============================================================================


class Orchestrator:
    """
    Main orchestrator for the ARCH system.

    Manages the complete lifecycle: startup, running, and shutdown.
    """

    def __init__(
        self,
        config_path: Path,
        keep_worktrees: bool = False,
    ):
        """
        Initialize the orchestrator.

        Args:
            config_path: Path to arch.yaml.
            keep_worktrees: If True, don't remove worktrees on shutdown.
        """
        self.config_path = Path(config_path)
        self.keep_worktrees = keep_worktrees

        # Will be initialized during startup
        self.config: Optional[ArchConfig] = None
        self.state: Optional[StateStore] = None
        self.token_tracker: Optional[TokenTracker] = None
        self.worktree_manager: Optional[WorktreeManager] = None
        self.session_manager: Optional[SessionManager] = None
        self.mcp_server: Optional[MCPServer] = None

        # Runtime state
        self._running = False
        self._shutdown_requested = False
        self._archie_session: Optional[AnySession] = None
        self._crash_restart_count = 0
        self._message_resume_count = 0
        self._archie_last_exit_time: Optional[float] = None
        self._archie_exit_handled = False
        self._project_complete = False
        self._github_enabled = False

        # Agent instance tracking: role -> count of active instances
        self._agent_instance_counts: dict[str, int] = {}
        # Agent ID counter for generating unique IDs
        self._agent_id_counter: int = 0

        # Signal handling
        self._original_sigint = None
        self._original_sigterm = None

    @property
    def state_dir(self) -> Path:
        """Get the state directory path."""
        if self.config:
            return Path(self.config.settings.state_dir)
        return Path(DEFAULT_STATE_DIR)

    @property
    def repo_path(self) -> Path:
        """Get the repository path."""
        if self.config:
            return Path(self.config.project.repo).resolve()
        return Path(".").resolve()

    async def startup(self) -> bool:
        """
        Execute the full startup sequence.

        Returns:
            True if startup succeeded, False otherwise.
        """
        logger.info("Starting ARCH...")

        try:
            # Step 1: Parse and validate config
            logger.info("Step 1: Parsing arch.yaml...")
            self.config = parse_config(self.config_path)
            logger.info(f"Project: {self.config.project.name}")

            # Step 2: Initialize state store
            logger.info("Step 2: Initializing state store...")
            state_dir = Path(self.config.settings.state_dir).resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            self.state = StateStore(state_dir)
            self.state.init_project(
                name=self.config.project.name,
                description=self.config.project.description,
                repo=str(self.repo_path),
            )

            # Initialize token tracker
            self.token_tracker = TokenTracker(state_dir=state_dir)

            # Step 3: Verify git repo
            logger.info("Step 3: Verifying git repository...")
            if not self._verify_git_repo():
                return False

            # Initialize worktree manager
            self.worktree_manager = WorktreeManager(self.repo_path)

            # Step 4: Permission gate
            logger.info("Step 4: Checking permission requirements...")
            if not await self._permission_gate():
                return False

            # Step 5: Container gate
            logger.info("Step 5: Checking container requirements...")
            if not await self._container_gate():
                return False

            # Step 6: GitHub gate
            logger.info("Step 6: Checking GitHub access...")
            await self._github_gate()

            # Step 7: Start MCP server
            logger.info("Step 7: Starting MCP server...")
            await self._start_mcp_server()

            # Initialize session manager
            self.session_manager = SessionManager(
                state=self.state,
                token_tracker=self.token_tracker,
                state_dir=state_dir,
                mcp_port=self.config.settings.mcp_port,
                on_output=self._on_agent_output,
                on_exit=self._on_agent_exit,
            )

            # Step 8: Create Archie's worktree
            logger.info("Step 8: Creating Archie's worktree...")
            await self._create_archie_worktree()

            # Step 9: Spawn Archie
            logger.info("Step 9: Spawning Archie...")
            if not await self._spawn_archie():
                return False

            # Step 10: Dashboard will be started separately (Step 9 implementation)
            logger.info("Step 10: Dashboard ready (implementation pending)")

            # Register signal handlers
            self._register_signal_handlers()

            self._running = True
            logger.info("ARCH startup complete!")
            return True

        except Exception as e:
            logger.error(f"Startup failed: {e}")
            await self.shutdown()
            return False

    async def shutdown(self, keep_worktrees: Optional[bool] = None) -> None:
        """
        Execute the full shutdown sequence.

        Args:
            keep_worktrees: Override the keep_worktrees setting.
        """
        if keep_worktrees is None:
            keep_worktrees = self.keep_worktrees

        logger.info("Shutting down ARCH...")
        self._shutdown_requested = True

        try:
            # Step 1: Stop all agent sessions
            if self.session_manager:
                logger.info("Stopping all agent sessions...")
                stopped = await self.session_manager.stop_all(timeout=DEFAULT_SHUTDOWN_TIMEOUT)
                logger.info(f"Stopped {stopped} sessions")

            # Step 2: Stop MCP server
            if self.mcp_server:
                logger.info("Stopping MCP server...")
                await self.mcp_server.stop()

            # Step 3: Remove worktrees
            if self.worktree_manager and not keep_worktrees:
                logger.info("Removing worktrees...")
                removed = self.worktree_manager.cleanup_all()
                logger.info(f"Removed {removed} worktrees")

            # Step 4: Final state is auto-persisted by StateStore
            # (StateStore auto-saves on every mutation)
            logger.info("State persisted")

            # Step 5: Print cost summary
            self._print_cost_summary()

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        finally:
            self._running = False
            self._restore_signal_handlers()

        logger.info("ARCH shutdown complete")

    async def run(self) -> None:
        """
        Main run loop. Blocks until shutdown is requested.
        """
        logger.info("ARCH is running. Press Ctrl+C to stop.")

        while self._running and not self._shutdown_requested:
            await asyncio.sleep(1)

            # Skip agent management if project is complete (waiting for user to quit)
            if self._project_complete:
                continue

            # Check if Archie needs handling after exit (only once per exit)
            if (self._archie_session
                    and not self._archie_session.is_running
                    and not self._archie_exit_handled):
                self._archie_exit_handled = True
                await self._handle_archie_exit()

            # Auto-resume: if Archie is not running and has unread messages, resume
            await self._check_auto_resume()

    async def _check_auto_resume(self) -> None:
        """
        Check if Archie should be auto-resumed due to unread messages.

        Conditions:
        - Archie is not running (exited gracefully or crashed)
        - Cooldown period (10s) has passed since last exit
        - There are unread messages for Archie
        - Haven't exceeded restart limits
        """
        # Skip if Archie is running or shutdown requested
        if self._shutdown_requested:
            return
        if self._archie_session and self._archie_session.is_running:
            return

        # Skip if no exit time recorded (Archie never ran)
        if self._archie_last_exit_time is None:
            return

        # Skip if within cooldown period (10 seconds)
        cooldown_seconds = 10
        if time.time() - self._archie_last_exit_time < cooldown_seconds:
            return

        # Skip if excessive message resumes (safety valve)
        if self._message_resume_count > 50:
            logger.warning("Excessive message resumes, stopping auto-resume")
            return

        # Check for unread messages
        if not self.state.has_unread_messages_for("archie"):
            return

        # Resume Archie
        logger.info("Auto-resuming Archie due to unread messages")
        await self._resume_archie_for_messages()

    def _verify_git_repo(self) -> bool:
        """Verify the git repository is accessible."""
        try:
            result = subprocess.run(
                ["git", "status"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                logger.error(f"Not a git repository: {self.repo_path}")
                return False
            return True
        except FileNotFoundError:
            logger.error("git not found")
            return False
        except subprocess.TimeoutExpired:
            logger.error("git status timed out")
            return False

    async def _permission_gate(self) -> bool:
        """
        Check and confirm skip_permissions usage.

        Returns:
            True if approved or no agents need it, False otherwise.
        """
        agents_needing_perms = check_permission_gate(self.config)

        if not agents_needing_perms:
            logger.info("No agents require skip_permissions")
            return True

        # Print prominent warning
        print("\n" + "=" * 60)
        print("⚠️  WARNING: DANGEROUS PERMISSIONS REQUESTED")
        print("=" * 60)
        print("\nThe following agent roles have skip_permissions enabled:")
        for agent_id in agents_needing_perms:
            print(f"  • {agent_id}")
        print("\nThis allows these agents to execute commands without")
        print("confirmation, which could be dangerous.")
        print("\nDo you want to continue? [y/N]: ", end="", flush=True)

        # Get user confirmation
        try:
            response = input().strip().lower()
        except EOFError:
            response = ""

        if response != "y":
            logger.info("User declined skip_permissions")
            return False

        # Log acknowledgment
        self._log_permission_acknowledgment(agents_needing_perms)
        logger.info("skip_permissions approved by user")
        return True

    def _log_permission_acknowledgment(self, agent_ids: list[str]) -> None:
        """Log permission acknowledgment to state directory."""
        audit_path = self.state_dir / "permissions_audit.log"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with open(audit_path, "a") as f:
            for agent_id in agent_ids:
                f.write(f"{timestamp}  STARTUP_APPROVAL  agent_id={agent_id}  approved_by=user\n")

    async def _container_gate(self) -> bool:
        """
        Check Docker availability and images.

        Returns:
            True if containers are ready or not needed, False otherwise.
        """
        docker_ok, sandboxed_agents, missing_images = check_container_gate(self.config)

        if not sandboxed_agents:
            logger.info("No agents require containers")
            return True

        if not docker_ok:
            logger.error("Docker is required but not available")
            print("\n❌ Docker daemon is not running.")
            print("Start Docker and try again.")
            return False

        logger.info(f"Containerized agents: {', '.join(sandboxed_agents)}")

        # Pull missing images
        for image in missing_images:
            logger.info(f"Pulling image: {image}")
            success, msg = pull_image(image)
            if not success:
                logger.error(f"Failed to pull {image}: {msg}")
                print(f"\n❌ Failed to pull Docker image: {image}")
                print(f"   {msg}")
                print("\nBuild or pull the image manually and try again.")
                return False

        return True

    async def _github_gate(self) -> None:
        """Check GitHub availability (warn only, don't fail)."""
        available, msg = check_github_gate(self.config)

        if available:
            logger.info(msg)
            self._github_enabled = True
        else:
            logger.warning(f"GitHub integration disabled: {msg}")
            self._github_enabled = False
            if self.config.github:
                print(f"\n⚠️  GitHub integration disabled: {msg}")
                print("GitHub tools will not be available for this session.")

    async def _start_mcp_server(self) -> None:
        """Start the MCP server with lifecycle callbacks."""
        self.mcp_server = MCPServer(
            state=self.state,
            port=self.config.settings.mcp_port,
            repo_path=self.repo_path,
            github_repo=self.config.github.repo if self.config.github and self._github_enabled else None,
            on_spawn_agent=self._handle_spawn_agent,
            on_teardown_agent=self._handle_teardown_agent,
            on_request_merge=self._handle_request_merge,
            on_close_project=self._handle_close_project,
            on_plan_team=self._handle_plan_team,
        )
        await self.mcp_server.start()
        logger.info(f"MCP server listening on port {self.config.settings.mcp_port}")

    async def _create_archie_worktree(self) -> None:
        """Create Archie's worktree and write CLAUDE.md."""
        # Create worktree
        worktree_path = self.worktree_manager.create("archie")

        # Read persona file
        persona_path = self.repo_path / self.config.archie.persona
        if persona_path.exists():
            persona_content = persona_path.read_text()
        else:
            logger.warning(f"Archie persona not found at {persona_path}, using default")
            persona_content = "# Archie - Lead Agent\n\nYou are Archie, the lead agent."

        # Check for persisted session state (from previous run)
        session_state = None
        existing_archie = self.state.get_agent("archie")
        if existing_archie and existing_archie.get("context"):
            session_state = existing_archie["context"]
            logger.info("Injecting session state from previous Archie session")

        # Write CLAUDE.md with injected context
        self.worktree_manager.write_claude_md(
            agent_id="archie",
            persona_content=persona_content,
            assignment=f"Lead the {self.config.project.name} project",
            project_name=self.config.project.name,
            project_description=self.config.project.description,
            active_agents={},  # No other agents yet
            available_tools=[
                "send_message", "get_messages", "update_status", "save_progress", "report_completion",
                "spawn_agent", "teardown_agent", "list_agents", "escalate_to_user",
                "request_merge", "get_project_context", "close_project", "update_brief",
                "list_personas", "plan_team",
            ] + (["gh_create_issue", "gh_list_issues", "gh_close_issue", "gh_update_issue",
                  "gh_add_comment", "gh_create_milestone", "gh_list_milestones"]
                 if self._github_enabled else []),
            session_state=session_state,
        )

        logger.info(f"Archie worktree created at {worktree_path}")

    async def _spawn_archie(self) -> bool:
        """Spawn the Archie session."""
        worktree_path = self.worktree_manager.get_worktree_path("archie")
        if not worktree_path:
            logger.error("Archie worktree not found")
            return False

        # Register Archie in state
        self.state.register_agent(
            agent_id="archie",
            role="lead",
            worktree=str(worktree_path),
            sandboxed=False,
            skip_permissions=False,
        )

        # Create agent config
        config = self._build_archie_config()

        # Build initial prompt
        prompt = self._build_archie_prompt()

        # Spawn session
        self._archie_session = await self.session_manager.spawn(config, prompt)

        if not self._archie_session:
            logger.error("Failed to spawn Archie session")
            return False

        logger.info("Archie is online")
        return True

    def _build_archie_prompt(self) -> str:
        """Build the initial prompt for Archie."""
        prompt_parts = [
            f"You are Archie, leading the {self.config.project.name} project.",
            f"\nProject description: {self.config.project.description}",
            "\nStart by calling get_project_context to understand the current state.",
            "Read BRIEF.md to understand the goals and current status.",
        ]

        if self._github_enabled:
            prompt_parts.append(
                "\nGitHub integration is enabled. Use gh_list_milestones and "
                "gh_list_issues to understand the sprint state."
            )

        prompt_parts.append(
            "\nWhen ready, spawn agents from the pool to work on tasks. "
            "Coordinate their work and merge completed branches."
        )

        return "\n".join(prompt_parts)

    def _build_archie_config(self) -> AgentConfig:
        """Build the AgentConfig for Archie, used by all spawn/resume paths."""
        worktree_path = self.worktree_manager.get_worktree_path("archie")
        archie_allowed_tools = list(DEFAULT_ALLOWED_TOOLS_ARCHIE)

        return AgentConfig(
            agent_id="archie",
            role="lead",
            model=self.config.archie.model,
            worktree=str(worktree_path),
            sandboxed=False,
            skip_permissions=False,
            allowed_tools=archie_allowed_tools,
            permission_prompt_tool="mcp__arch__handle_permission_request",
        )

    async def _on_agent_output(self, agent_id: str, event: dict[str, Any]) -> None:
        """Log agent stream events to provide visibility into agent thinking."""
        event_type = event.get("type", "")

        if event_type == "assistant":
            # Extract text content from assistant messages
            content_blocks = (event.get("message") or {}).get("content", [])
            for block in content_blocks:
                if block.get("type") == "text":
                    text = block["text"]
                    # Truncate long text for readability
                    if len(text) > 200:
                        text = text[:200] + "..."
                    logger.info(f"[{agent_id}] {text}")
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "unknown")
                    logger.info(f"[{agent_id}] calling tool: {tool_name}")

    async def _on_agent_exit(self, agent_id: str, exit_code: int) -> None:
        """Handle agent exit callback."""
        logger.info(f"Agent {agent_id} exited with code {exit_code}")

        # Decrement instance count for this agent's role
        agent = self.state.get_agent(agent_id)
        if agent and agent.get("role"):
            role = agent["role"]
            if role in self._agent_instance_counts:
                self._agent_instance_counts[role] = max(0, self._agent_instance_counts[role] - 1)

        if agent_id == "archie" and exit_code != 0:
            # Archie exited unexpectedly - will be handled by run loop
            pass

    # =========================================================================
    # Agent Lifecycle Handlers (called by MCP server)
    # =========================================================================

    def _get_pool_entry(self, role: str) -> Optional[AgentPoolEntry]:
        """Look up an agent pool entry by role ID."""
        for entry in self.config.agent_pool:
            if entry.id == role:
                return entry
        return None

    def _generate_agent_id(self, role: str) -> str:
        """Generate a unique agent ID for a role."""
        self._agent_id_counter += 1
        return f"{role}-{self._agent_id_counter}"

    async def _handle_spawn_agent(
        self,
        role: str,
        assignment: str,
        context: Optional[str] = None,
        skip_permissions: bool = False,
    ) -> dict[str, Any]:
        """
        Handle spawn_agent request from Archie.

        Creates worktree, writes CLAUDE.md, spawns session.
        """
        logger.info(f"Spawn request: role={role}, assignment={assignment[:50]}...")

        # Look up role in agent pool
        pool_entry = self._get_pool_entry(role)
        if not pool_entry:
            logger.error(f"Unknown role: {role}")
            return {"error": f"Unknown role: {role}. Available roles: {[e.id for e in self.config.agent_pool]}"}

        # Check max instances
        current_count = self._agent_instance_counts.get(role, 0)
        if current_count >= pool_entry.max_instances:
            logger.warning(f"Max instances ({pool_entry.max_instances}) reached for role {role}")
            return {"error": f"Max instances ({pool_entry.max_instances}) reached for role {role}"}

        # Check max concurrent agents
        running_count = len(self.session_manager.list_running_sessions())
        if running_count >= self.config.settings.max_concurrent_agents:
            logger.warning(f"Max concurrent agents ({self.config.settings.max_concurrent_agents}) reached")
            return {"error": f"Max concurrent agents ({self.config.settings.max_concurrent_agents}) reached"}

        # Validate skip_permissions request
        actual_skip_permissions = False
        if skip_permissions:
            if pool_entry.permissions.skip_permissions:
                actual_skip_permissions = True
            else:
                logger.warning(f"skip_permissions requested for {role} but not configured in pool")

        # Generate unique agent ID
        agent_id = self._generate_agent_id(role)

        try:
            # Create worktree
            logger.info(f"Creating worktree for {agent_id}...")
            worktree_path = self.worktree_manager.create(agent_id)

            # Read persona file
            persona_path = self.repo_path / pool_entry.persona
            if persona_path.exists():
                persona_content = persona_path.read_text()
            else:
                logger.warning(f"Persona not found at {persona_path}, using default")
                persona_content = f"# {role}\n\nYou are a {role} agent."

            # Build available tools list (worker tools only)
            available_tools = ["send_message", "get_messages", "update_status", "save_progress", "report_completion"]

            # Write CLAUDE.md
            full_assignment = assignment
            if context:
                full_assignment = f"{assignment}\n\nAdditional context:\n{context}"

            # Get active agents for context
            active_agents = {
                a["id"]: a["role"]
                for a in self.state.list_agents()
                if a["status"] not in ("done", "error")
            }

            # Check for persisted session state (from previous run)
            session_state = None
            existing_agent = self.state.get_agent(agent_id)
            if existing_agent and existing_agent.get("context"):
                session_state = existing_agent["context"]
                logger.info(f"Injecting session state for {agent_id}")

            self.worktree_manager.write_claude_md(
                agent_id=agent_id,
                persona_content=persona_content,
                assignment=full_assignment,
                project_name=self.config.project.name,
                project_description=self.config.project.description,
                active_agents=active_agents,
                available_tools=available_tools,
                session_state=session_state,
            )

            # Register agent in state
            self.state.register_agent(
                agent_id=agent_id,
                role=role,
                worktree=str(worktree_path),
                sandboxed=pool_entry.sandbox.enabled,
                skip_permissions=actual_skip_permissions,
            )

            # Build allowed tools list: defaults + user config
            agent_allowed_tools = list(DEFAULT_ALLOWED_TOOLS_ALL)
            if pool_entry.permissions.allowed_tools:
                # Add user-configured tools, avoiding duplicates
                for tool in pool_entry.permissions.allowed_tools:
                    if tool not in agent_allowed_tools:
                        agent_allowed_tools.append(tool)

            # Build AgentConfig
            agent_config = AgentConfig(
                agent_id=agent_id,
                role=role,
                model=pool_entry.model,
                worktree=str(worktree_path),
                sandboxed=pool_entry.sandbox.enabled,
                skip_permissions=actual_skip_permissions,
                allowed_tools=agent_allowed_tools,
                permission_prompt_tool="mcp__arch__handle_permission_request",
                container_image=pool_entry.sandbox.image,
                container_memory_limit=pool_entry.sandbox.memory_limit,
                container_cpus=pool_entry.sandbox.cpus,
                container_network=pool_entry.sandbox.network,
                container_extra_mounts=pool_entry.sandbox.extra_mounts,
            )

            # Spawn session
            logger.info(f"Spawning session for {agent_id}...")
            session = await self.session_manager.spawn(agent_config, full_assignment)

            if not session:
                # Cleanup on failure
                self.worktree_manager.remove(agent_id)
                self.state.remove_agent(agent_id)
                return {"error": f"Failed to spawn session for {agent_id}"}

            # Update instance count
            self._agent_instance_counts[role] = current_count + 1

            logger.info(f"Agent {agent_id} spawned successfully")
            return {
                "agent_id": agent_id,
                "worktree_path": str(worktree_path),
                "sandboxed": pool_entry.sandbox.enabled,
                "skip_permissions": actual_skip_permissions,
                "status": "spawning"
            }

        except Exception as e:
            logger.error(f"Failed to spawn agent {agent_id}: {e}")
            # Cleanup
            try:
                self.worktree_manager.remove(agent_id)
            except Exception:
                pass
            try:
                self.state.remove_agent(agent_id)
            except Exception:
                pass
            return {"error": str(e)}

    async def _handle_teardown_agent(self, agent_id: str) -> bool:
        """
        Handle teardown_agent request from Archie.

        Stops session and removes worktree.
        """
        logger.info(f"Teardown request for agent: {agent_id}")

        if agent_id == "archie":
            logger.warning("Cannot teardown Archie")
            return False

        try:
            # Stop the session
            if self.session_manager:
                stopped = await self.session_manager.stop(agent_id)
                if stopped:
                    logger.info(f"Stopped session for {agent_id}")
                self.session_manager.remove_session(agent_id)

            # Auto-merge unmerged work before removing worktree
            if self.worktree_manager and self.worktree_manager.exists(agent_id):
                try:
                    branch = f"agent/{agent_id}"
                    # Check if branch has unmerged commits
                    result = subprocess.run(
                        ["git", "log", "main.." + branch, "--oneline"],
                        cwd=self.worktree_manager.repo_path,
                        capture_output=True, text=True
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        logger.info(f"Auto-merging unmerged work from {agent_id}")
                        self.worktree_manager.merge(agent_id, "main")
                except Exception as e:
                    logger.warning(f"Auto-merge failed for {agent_id}: {e}")

            # Remove worktree (unless keep_worktrees is set)
            if self.worktree_manager and not self.keep_worktrees:
                try:
                    self.worktree_manager.remove(agent_id)
                    logger.info(f"Removed worktree for {agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to remove worktree for {agent_id}: {e}")

            # Update state
            if self.state:
                self.state.remove_agent(agent_id)

            return True

        except Exception as e:
            logger.error(f"Failed to teardown agent {agent_id}: {e}")
            return False

    async def _handle_request_merge(
        self,
        agent_id: str,
        target_branch: str = "main",
        pr_title: Optional[str] = None,
        pr_body: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Handle request_merge request from Archie.

        Merges worktree branch or creates a PR.
        """
        logger.info(f"Merge request for agent: {agent_id} -> {target_branch}")

        if not self.worktree_manager:
            return {"status": "rejected", "error": "Worktree manager not available"}

        # Check if worktree exists
        worktree_path = self.worktree_manager.get_worktree_path(agent_id)
        if not worktree_path:
            return {"status": "rejected", "error": f"No worktree found for {agent_id}"}

        try:
            if pr_title:
                # Create PR instead of direct merge
                logger.info(f"Creating PR: {pr_title}")
                pr_url = self.worktree_manager.create_pr(
                    agent_id=agent_id,
                    target_branch=target_branch,
                    title=pr_title,
                    body=pr_body or "",
                )
                if pr_url:
                    return {"status": "approved", "pr_url": pr_url}
                else:
                    return {"status": "rejected", "error": "Failed to create PR"}
            else:
                # Direct merge
                # Check if auto_merge is enabled or require approval
                if "merge" in self.config.settings.require_user_approval and not self.config.settings.auto_merge:
                    # Would need escalation - for now just log
                    logger.info(f"Merge requires approval (not implemented in this step)")
                    # TODO: Integrate with escalate_to_user when dashboard is ready

                logger.info(f"Merging {agent_id} into {target_branch}")
                success = self.worktree_manager.merge(agent_id, target_branch)
                if success:
                    return {"status": "approved"}
                else:
                    return {"status": "rejected", "error": "Merge failed"}

        except Exception as e:
            logger.error(f"Merge failed for {agent_id}: {e}")
            return {"status": "rejected", "error": str(e)}

    async def _handle_close_project(self, summary: str) -> bool:
        """
        Handle close_project request from Archie.

        Marks the project as complete but keeps the dashboard open
        so the user can review results before pressing q to quit.
        """
        logger.info(f"Close project requested: {summary}")
        self._project_complete = True

        # Store summary in state so dashboard can display it
        self.state.update_project(status="complete", summary=summary)

        # Add a system message so it shows in the activity log
        self.state.add_message(
            from_agent="archie",
            to_agent="system",
            content=f"Project complete: {summary}",
        )

        return True

    async def _handle_plan_team(
        self,
        agents: list[dict[str, str]],
        summary: str
    ) -> dict[str, Any]:
        """
        Handle plan_team request from Archie.

        Validates the proposed team and either auto-approves or escalates
        to the user. On approval, adds entries to the runtime agent_pool.
        """
        logger.info(f"Team plan received: {len(agents)} agents — {summary}")

        # Format the plan for display
        plan_lines = [f"Team Plan: {summary}", ""]
        for a in agents:
            plan_lines.append(f"  - {a['role']} ({a['persona']}): {a['rationale']}")

        plan_text = "\n".join(plan_lines)

        if not self.config.settings.auto_approve_team:
            # Escalate to user for approval
            if not self.mcp_server:
                return {"error": "MCP server not available for escalation"}

            question = (
                f"Archie proposes the following team:\n\n{plan_text}\n\n"
                f"Approve this team?"
            )
            answer = await self.mcp_server._escalate_and_wait(
                question=question,
                options=["Yes", "No"]
            )

            if answer.lower() not in ("yes", "y", "a"):
                logger.info("Team plan rejected by user")
                return {"approved": False, "reason": "User rejected the team plan"}

        # Approved — add to runtime agent_pool
        for a in agents:
            role = a["role"]
            persona = a["persona"]

            # Check if already in pool
            if self._get_pool_entry(role):
                continue

            self.config.agent_pool.append(AgentPoolEntry(
                id=role,
                persona=persona,
                model=self.config.archie.model,  # Use same model as archie by default
            ))
            logger.info(f"Added {role} ({persona}) to agent pool")

        # Log approval
        self.state.add_message(
            from_agent="system",
            to_agent="archie",
            content=f"Team plan approved. {len(agents)} roles available: "
                    f"{', '.join(a['role'] for a in agents)}"
        )

        return {
            "approved": True,
            "roles": [a["role"] for a in agents],
        }

    async def _handle_archie_exit(self) -> None:
        """Handle Archie session exit.

        In --print mode, exit code 0 is normal (prompt completed).
        Only treat non-zero exit codes as crashes requiring immediate restart.
        """
        # Record exit time for auto-resume cooldown
        self._archie_last_exit_time = time.time()

        if self._shutdown_requested:
            return

        # Check exit code to distinguish normal completion from crash
        exit_code = None
        if self._archie_session:
            exit_code = self._archie_session.exit_code

        if exit_code is None or exit_code == 0:
            # Normal exit (--print mode completed prompt).
            # Don't restart immediately. Let auto-resume handle future messages.
            logger.info("Archie completed prompt and exited normally")
            self._crash_restart_count = 0  # Reset on successful completion
            return

        # Non-zero exit code: this is a crash. Attempt restart.
        self._crash_restart_count += 1

        if self._crash_restart_count > 2:
            logger.error("Archie has crashed multiple times")
            print("\n--- Archie has crashed multiple times. Shutting down.")
            self._shutdown_requested = True
            return

        logger.warning(f"Archie exited with code {exit_code}, attempting restart...")

        session_id = self._archie_session.session_id if self._archie_session else None

        if session_id:
            logger.info(f"Resuming Archie session: {session_id}")
            config = self._build_archie_config()

            self._archie_session = await self.session_manager.spawn(
                config,
                "Resume previous work",
                resume_session_id=session_id,
            )

            if self._archie_session:
                self._archie_exit_handled = False  # Reset for new session
                logger.info("Archie restarted successfully")
            else:
                logger.error("Failed to restart Archie")
                print("\n--- Failed to restart Archie. Shutting down.")
                self._shutdown_requested = True
        else:
            logger.error("No session ID available for Archie restart")
            print("\n--- Cannot restart Archie (no session ID). Shutting down.")
            self._shutdown_requested = True

    async def _resume_archie_for_messages(self) -> None:
        """
        Resume Archie to handle unread messages.

        Called by auto-resume logic when messages arrive while Archie is idle.
        """
        self._message_resume_count += 1

        session_id = self._archie_session.session_id if self._archie_session else None
        worktree_path = self.worktree_manager.get_worktree_path("archie")

        if not worktree_path:
            logger.error("Cannot resume Archie: worktree not found")
            return

        config = self._build_archie_config()

        # Resume with a prompt about unread messages
        prompt = "You have unread messages. Call get_messages and take action."

        if session_id:
            self._archie_session = await self.session_manager.spawn(
                config,
                prompt,
                resume_session_id=session_id,
            )
        else:
            # No session to resume, spawn fresh
            self._archie_session = await self.session_manager.spawn(config, prompt)

        if self._archie_session:
            self._archie_exit_handled = False  # Reset for new session
            logger.info("Archie resumed for message handling")
        else:
            logger.error("Failed to resume Archie for messages")

    def _print_cost_summary(self) -> None:
        """Print cost summary to stdout."""
        if not self.token_tracker:
            return

        print("\n" + "=" * 40)
        print("COST SUMMARY")
        print("=" * 40)

        all_usage = self.token_tracker.get_all_usage()
        total_cost = 0.0

        for agent_id, usage in all_usage.items():
            cost = usage.get("cost_usd", 0.0)
            total_cost += cost
            print(f"{agent_id:20} ${cost:.4f}")

        print("-" * 40)
        print(f"{'Total':20} ${total_cost:.4f}")

        if self.config and self.config.settings.token_budget_usd:
            budget = self.config.settings.token_budget_usd
            pct = (total_cost / budget) * 100 if budget > 0 else 0
            print(f"{'Budget':20} ${budget:.2f} ({pct:.1f}% used)")

        print("=" * 40 + "\n")

    def _register_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""
        self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)
        self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
        atexit.register(self._atexit_handler)

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        if self._original_sigint:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm:
            signal.signal(signal.SIGTERM, self._original_sigterm)

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM."""
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name}, initiating shutdown...")
        self._shutdown_requested = True

    def _atexit_handler(self) -> None:
        """Handle atexit for emergency cleanup."""
        if self._running:
            logger.warning("Emergency cleanup on exit")
            # Synchronous cleanup
            if self.worktree_manager and not self.keep_worktrees:
                try:
                    self.worktree_manager.cleanup_all()
                except Exception as e:
                    logger.error(f"Worktree cleanup failed: {e}")


# ============================================================================
# Convenience Functions
# ============================================================================


async def run_arch(
    config_path: Path = Path("arch.yaml"),
    keep_worktrees: bool = False,
) -> int:
    """
    Run ARCH with the given configuration.

    Args:
        config_path: Path to arch.yaml.
        keep_worktrees: If True, don't remove worktrees on shutdown.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    orchestrator = Orchestrator(config_path, keep_worktrees)

    try:
        if not await orchestrator.startup():
            return 1

        await orchestrator.run()
        return 0

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        return 0

    finally:
        await orchestrator.shutdown()
