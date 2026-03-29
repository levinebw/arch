"""
ARCH Worktree Manager

Manages isolated git worktrees for each agent. Each agent gets its own
worktree with a dedicated branch, enabling parallel development without
conflicts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import git
from git import Repo
from git.exc import GitCommandError


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""
    pass


class WorktreeManager:
    """
    Manages git worktrees for ARCH agents.

    Each agent operates in an isolated worktree at .worktrees/{agent_id}/
    on branch agent/{agent_id}. This allows parallel development without
    merge conflicts until work is ready to be integrated.
    """

    WORKTREE_DIR = ".worktrees"
    BRANCH_PREFIX = "agent"

    def __init__(self, repo_path: str | Path):
        """
        Initialize the worktree manager.

        Args:
            repo_path: Path to the git repository root.

        Raises:
            WorktreeError: If the path is not a valid git repository.
        """
        self.repo_path = Path(repo_path).resolve()

        try:
            self.repo = Repo(self.repo_path)
        except git.InvalidGitRepositoryError:
            raise WorktreeError(f"Not a git repository: {self.repo_path}")

        # Ensure worktree directory exists
        self.worktree_base = self.repo_path / self.WORKTREE_DIR
        self.worktree_base.mkdir(exist_ok=True)

    def _worktree_path(self, agent_id: str) -> Path:
        """Get the worktree path for an agent."""
        return self.worktree_base / agent_id

    def _branch_name(self, agent_id: str) -> str:
        """Get the branch name for an agent."""
        return f"{self.BRANCH_PREFIX}/{agent_id}"

    def create(
        self,
        agent_id: str,
        base_branch: Optional[str] = None
    ) -> Path:
        """
        Create a worktree for an agent.

        Creates a new worktree at .worktrees/{agent_id}/ on a new branch
        agent/{agent_id} based on the specified base branch (or current HEAD).

        Args:
            agent_id: Unique agent identifier.
            base_branch: Branch to base the worktree on (default: current HEAD).

        Returns:
            Path to the created worktree.

        Raises:
            WorktreeError: If worktree creation fails.
        """
        worktree_path = self._worktree_path(agent_id)
        branch_name = self._branch_name(agent_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        try:
            # Build the git worktree add command
            cmd = ["git", "worktree", "add", str(worktree_path), "-b", branch_name]

            if base_branch:
                cmd.append(base_branch)

            subprocess.run(
                cmd,
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            return worktree_path

        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Failed to create worktree: {e.stderr}")

    def write_claude_md(
        self,
        agent_id: str,
        persona_content: str,
        project_name: str,
        project_description: str,
        assignment: str,
        active_agents: Optional[list[tuple[str, str]]] = None,
        available_tools: Optional[list[str]] = None,
        session_state: Optional[dict] = None
    ) -> Path:
        """
        Write CLAUDE.md to an agent's worktree with injected context.

        Args:
            agent_id: Agent identifier.
            persona_content: Original persona markdown content.
            project_name: Project name from config.
            project_description: Project description from config.
            assignment: Task description for this agent.
            active_agents: List of (agent_id, role) tuples for active team members.
            available_tools: List of available MCP tool names.
            session_state: Optional persisted context from previous session
                          (progress, files_modified, next_steps, blockers, decisions).

        Returns:
            Path to the written CLAUDE.md file.

        Raises:
            WorktreeError: If worktree doesn't exist or write fails.
        """
        worktree_path = self._worktree_path(agent_id)

        if not worktree_path.exists():
            raise WorktreeError(f"Worktree does not exist: {worktree_path}")

        # Format active agents list
        # active_agents can be list of tuples or dict
        if active_agents:
            if isinstance(active_agents, dict):
                agents_str = ", ".join(f"{aid}: {role}" for aid, role in active_agents.items())
            else:
                agents_str = ", ".join(f"{aid}: {role}" for aid, role in active_agents)
        else:
            agents_str = "(none yet)"

        # Format available tools
        if available_tools:
            tools_str = ", ".join(available_tools)
        else:
            tools_str = "send_message, get_messages, update_status, report_completion"

        # Build session state section if present
        session_state_section = ""
        if session_state:
            session_state_section = "\n## Session State (from previous session)\n"
            if session_state.get("progress"):
                session_state_section += f"- **Progress:** {session_state['progress']}\n"
            if session_state.get("files_modified"):
                files = ", ".join(session_state["files_modified"])
                session_state_section += f"- **Files modified:** {files}\n"
            if session_state.get("next_steps"):
                session_state_section += f"- **Next steps:** {session_state['next_steps']}\n"
            if session_state.get("blockers"):
                session_state_section += f"- **Blockers:** {session_state['blockers']}\n"
            if session_state.get("decisions"):
                decisions = "; ".join(session_state["decisions"])
                session_state_section += f"- **Decisions:** {decisions}\n"

        # Build injected header
        header = f"""<!-- INJECTED BY ARCH — DO NOT EDIT BELOW THIS LINE -->
## ARCH Harness Context
- **Your agent ID:** {agent_id}
- **Project:** {project_name} — {project_description}
- **Your worktree path:** {worktree_path}
- **Available MCP tools (via "arch" server):** {tools_str}
- **Active team members:** {agents_str}
- **Your assignment:** {assignment}
<!-- END ARCH CONTEXT -->
{session_state_section}
---

{persona_content}"""

        # Write to .claude/CLAUDE.md (not root CLAUDE.md) to avoid
        # overwriting the project's own CLAUDE.md on merge
        claude_dir = worktree_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_md_path = claude_dir / "CLAUDE.md"
        claude_md_path.write_text(header)

        return claude_md_path

    def setup_agent_skills(
        self,
        agent_id: str,
        skills_source_dir: Path,
    ) -> list[str]:
        """
        Copy skills from a persona directory into the agent's worktree.

        Skills are placed at .worktrees/{agent_id}/.claude/skills/ so that
        Claude Code discovers them natively at session start.

        Args:
            agent_id: Agent identifier.
            skills_source_dir: Path to the persona's skills/ directory
                              (e.g., repo/personas/engineering/skills/).

        Returns:
            List of skill names that were copied.

        Raises:
            WorktreeError: If worktree doesn't exist.
        """
        worktree_path = self._worktree_path(agent_id)
        if not worktree_path.exists():
            raise WorktreeError(f"Worktree does not exist: {worktree_path}")

        if not skills_source_dir.is_dir():
            return []

        target_skills_dir = worktree_path / ".claude" / "skills"
        target_skills_dir.mkdir(parents=True, exist_ok=True)

        copied_skills = []
        for skill_dir in sorted(skills_source_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            target = target_skills_dir / skill_dir.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill_dir, target)
            copied_skills.append(skill_dir.name)

        return copied_skills

    def remove(self, agent_id: str, force: bool = True) -> bool:
        """
        Remove an agent's worktree.

        Args:
            agent_id: Agent identifier.
            force: Force removal even if worktree has uncommitted changes.

        Returns:
            True if worktree was removed successfully.

        Raises:
            WorktreeError: If removal fails.
        """
        worktree_path = self._worktree_path(agent_id)
        branch_name = self._branch_name(agent_id)

        if not worktree_path.exists():
            return False

        try:
            # Remove the worktree
            cmd = ["git", "worktree", "remove", str(worktree_path)]
            if force:
                cmd.append("--force")

            subprocess.run(
                cmd,
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            # Optionally delete the branch (don't fail if it doesn't exist)
            try:
                subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    cwd=self.repo_path,
                    check=True,
                    capture_output=True,
                    text=True
                )
            except subprocess.CalledProcessError:
                # Branch might not exist or might not be fully merged
                pass

            return True

        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Failed to remove worktree: {e.stderr}")

    def list_worktrees(self) -> list[dict[str, str]]:
        """
        List all agent worktrees.

        Returns:
            List of dicts with 'agent_id', 'path', and 'branch' keys.
        """
        result = []

        if not self.worktree_base.exists():
            return result

        for path in self.worktree_base.iterdir():
            if path.is_dir():
                agent_id = path.name
                branch_name = self._branch_name(agent_id)

                # Verify it's actually a worktree by checking for .git file
                git_file = path / ".git"
                if git_file.exists():
                    result.append({
                        "agent_id": agent_id,
                        "path": str(path),
                        "branch": branch_name
                    })

        return result

    def exists(self, agent_id: str) -> bool:
        """Check if a worktree exists for an agent."""
        worktree_path = self._worktree_path(agent_id)
        git_file = worktree_path / ".git"
        return git_file.exists()

    def auto_commit(self, agent_id: str) -> bool:
        """
        Auto-commit any uncommitted changes in an agent's worktree.

        Checks for staged/unstaged/untracked files and commits them all
        so they aren't lost when the worktree is removed.

        Returns:
            True if changes were committed, False if worktree was clean.

        Raises:
            WorktreeError: If commit fails.
        """
        worktree_path = self._worktree_path(agent_id)
        if not worktree_path.exists():
            return False

        try:
            # Check for any changes (staged, unstaged, or untracked)
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree_path,
                capture_output=True, text=True, check=True
            )

            if not status.stdout.strip():
                return False  # Clean worktree

            # Stage everything
            subprocess.run(
                ["git", "add", "-A"],
                cwd=worktree_path,
                check=True, capture_output=True, text=True
            )

            # Commit
            subprocess.run(
                ["git", "commit", "-m",
                 f"Auto-commit uncommitted work from {agent_id} before teardown"],
                cwd=worktree_path,
                check=True, capture_output=True, text=True
            )

            return True

        except subprocess.CalledProcessError as e:
            raise WorktreeError(
                f"Auto-commit failed for {agent_id}: {e.stderr}"
            )

    def get_worktree_path(self, agent_id: str) -> Optional[Path]:
        """Get the worktree path for an agent, or None if it doesn't exist."""
        if self.exists(agent_id):
            return self._worktree_path(agent_id)
        return None

    def merge(
        self,
        agent_id: str,
        target_branch: str = "main",
        summary: Optional[str] = None
    ) -> bool:
        """
        Merge an agent's worktree branch into the target branch.

        Uses --no-ff to preserve branch history and attribution.

        Args:
            agent_id: Agent whose branch to merge.
            target_branch: Branch to merge into (default: main).
            summary: Summary for the merge commit message.

        Returns:
            True if merge succeeded.

        Raises:
            WorktreeError: If merge fails (e.g., conflicts).
        """
        branch_name = self._branch_name(agent_id)

        if not self.exists(agent_id):
            raise WorktreeError(f"Worktree does not exist for agent: {agent_id}")

        try:
            # Checkout target branch in main repo
            subprocess.run(
                ["git", "checkout", target_branch],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            # Merge with --no-ff
            merge_msg = f"Merge {agent_id}"
            if summary:
                merge_msg += f": {summary}"

            subprocess.run(
                ["git", "merge", "--no-ff", branch_name, "-m", merge_msg],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            return True

        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Merge failed: {e.stderr}")

    def create_pr(
        self,
        agent_id: str,
        title: str,
        body: str,
        target_branch: str = "main"
    ) -> dict[str, str]:
        """
        Create a GitHub PR for an agent's worktree branch.

        Args:
            agent_id: Agent whose branch to create PR for.
            title: PR title.
            body: PR body/description.
            target_branch: Base branch for the PR (default: main).

        Returns:
            Dict with 'url' and 'number' of the created PR.

        Raises:
            WorktreeError: If PR creation fails or gh CLI unavailable.
        """
        branch_name = self._branch_name(agent_id)

        if not self.exists(agent_id):
            raise WorktreeError(f"Worktree does not exist for agent: {agent_id}")

        try:
            # First, push the branch to remote
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            # Create PR using gh CLI
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--title", title,
                    "--body", body,
                    "--head", branch_name,
                    "--base", target_branch
                ],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )

            # gh pr create outputs the PR URL
            pr_url = result.stdout.strip()

            # Extract PR number from URL (e.g., https://github.com/owner/repo/pull/42)
            pr_number = pr_url.split("/")[-1] if pr_url else ""

            return {
                "url": pr_url,
                "number": pr_number
            }

        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Failed to create PR: {e.stderr}")
        except FileNotFoundError:
            raise WorktreeError("gh CLI not found. Install GitHub CLI to create PRs.")

    def get_branch_status(self, agent_id: str) -> dict[str, any]:
        """
        Get the status of an agent's branch relative to target.

        Returns:
            Dict with 'ahead', 'behind', 'has_uncommitted' keys.
        """
        branch_name = self._branch_name(agent_id)
        worktree_path = self._worktree_path(agent_id)

        if not self.exists(agent_id):
            raise WorktreeError(f"Worktree does not exist for agent: {agent_id}")

        try:
            # Check for uncommitted changes in worktree
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree_path,
                check=True,
                capture_output=True,
                text=True
            )
            has_uncommitted = bool(status_result.stdout.strip())

            # Get ahead/behind counts relative to main
            rev_list = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", f"main...{branch_name}"],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )
            parts = rev_list.stdout.strip().split()
            behind = int(parts[0]) if len(parts) > 0 else 0
            ahead = int(parts[1]) if len(parts) > 1 else 0

            return {
                "ahead": ahead,
                "behind": behind,
                "has_uncommitted": has_uncommitted
            }

        except subprocess.CalledProcessError as e:
            raise WorktreeError(f"Failed to get branch status: {e.stderr}")

    def cleanup_all(self, force: bool = True) -> int:
        """
        Remove all agent worktrees.

        Args:
            force: Force removal even with uncommitted changes.

        Returns:
            Number of worktrees removed.
        """
        worktrees = self.list_worktrees()
        removed = 0

        for wt in worktrees:
            try:
                if self.remove(wt["agent_id"], force=force):
                    removed += 1
            except WorktreeError:
                # Continue cleaning up others even if one fails
                pass

        return removed
