"""Unit tests for ARCH Worktree Manager."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from arch.worktree import WorktreeManager, WorktreeError


class TestWorktreeManagerInit:
    """Tests for WorktreeManager initialization."""

    def test_init_with_valid_repo(self, git_repo):
        """WorktreeManager initializes with a valid git repo."""
        manager = WorktreeManager(git_repo)
        assert manager.repo_path == git_repo
        assert manager.worktree_base.exists()

    def test_init_with_invalid_path(self, tmp_path):
        """WorktreeManager raises error for non-git directory."""
        with pytest.raises(WorktreeError, match="Not a git repository"):
            WorktreeManager(tmp_path)

    def test_init_creates_worktree_dir(self, git_repo):
        """WorktreeManager creates .worktrees directory if needed."""
        worktree_dir = git_repo / ".worktrees"
        if worktree_dir.exists():
            worktree_dir.rmdir()

        manager = WorktreeManager(git_repo)
        assert worktree_dir.exists()


class TestWorktreeCreate:
    """Tests for worktree creation."""

    def test_create_worktree(self, worktree_manager):
        """create() creates a new worktree."""
        path = worktree_manager.create("frontend-1")

        assert path.exists()
        assert (path / ".git").exists()
        assert path == worktree_manager.worktree_base / "frontend-1"

    def test_create_worktree_creates_branch(self, worktree_manager):
        """create() creates the agent branch."""
        worktree_manager.create("backend-1")

        # Verify branch exists
        result = subprocess.run(
            ["git", "branch", "--list", "agent/backend-1"],
            cwd=worktree_manager.repo_path,
            capture_output=True,
            text=True
        )
        assert "agent/backend-1" in result.stdout

    def test_create_worktree_with_base_branch(self, worktree_manager):
        """create() can base worktree on a specific branch."""
        # Create a feature branch first
        subprocess.run(
            ["git", "checkout", "-b", "feature-base"],
            cwd=worktree_manager.repo_path,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=worktree_manager.repo_path,
            check=True,
            capture_output=True
        )

        path = worktree_manager.create("test-agent", base_branch="feature-base")
        assert path.exists()

    def test_create_duplicate_worktree_fails(self, worktree_manager):
        """create() fails if worktree already exists."""
        worktree_manager.create("duplicate")

        with pytest.raises(WorktreeError, match="already exists"):
            worktree_manager.create("duplicate")


class TestWorktreeClaudeMd:
    """Tests for CLAUDE.md writing."""

    def test_write_claude_md(self, worktree_manager):
        """write_claude_md creates CLAUDE.md with injected header."""
        worktree_manager.create("test-agent")

        path = worktree_manager.write_claude_md(
            agent_id="test-agent",
            persona_content="# Test Persona\n\nYou are a test agent.",
            project_name="Test Project",
            project_description="A test project",
            assignment="Run the tests"
        )

        assert path.exists()
        content = path.read_text()

        # Check injected header
        assert "<!-- INJECTED BY ARCH" in content
        assert "Your agent ID:** test-agent" in content
        assert "Test Project — A test project" in content
        assert "Your assignment:** Run the tests" in content
        assert "<!-- END ARCH CONTEXT -->" in content

        # Check persona content is included
        assert "# Test Persona" in content
        assert "You are a test agent." in content

    def test_write_claude_md_with_active_agents(self, worktree_manager):
        """write_claude_md includes active agents list."""
        worktree_manager.create("qa-1")

        path = worktree_manager.write_claude_md(
            agent_id="qa-1",
            persona_content="# QA",
            project_name="Test",
            project_description="Desc",
            assignment="Test things",
            active_agents=[("frontend-1", "frontend-dev"), ("backend-1", "backend-dev")]
        )

        content = path.read_text()
        assert "frontend-1: frontend-dev" in content
        assert "backend-1: backend-dev" in content

    def test_write_claude_md_with_custom_tools(self, worktree_manager):
        """write_claude_md includes custom tool list."""
        worktree_manager.create("archie")

        path = worktree_manager.write_claude_md(
            agent_id="archie",
            persona_content="# Archie",
            project_name="Test",
            project_description="Desc",
            assignment="Coordinate",
            available_tools=["spawn_agent", "teardown_agent", "escalate_to_user"]
        )

        content = path.read_text()
        assert "spawn_agent, teardown_agent, escalate_to_user" in content

    def test_write_claude_md_fails_without_worktree(self, worktree_manager):
        """write_claude_md fails if worktree doesn't exist."""
        with pytest.raises(WorktreeError, match="does not exist"):
            worktree_manager.write_claude_md(
                agent_id="nonexistent",
                persona_content="# Test",
                project_name="Test",
                project_description="Desc",
                assignment="Task"
            )

    def test_write_claude_md_with_session_state(self, worktree_manager):
        """write_claude_md injects session state section (Step 11.5)."""
        worktree_manager.create("resumed-agent")

        session_state = {
            "progress": "NavBar component complete, tests passing",
            "files_modified": ["src/Nav.tsx", "src/Nav.test.tsx"],
            "next_steps": "Wire up routing integration",
            "blockers": None,
            "decisions": ["Used React Router v6 over v5"]
        }

        path = worktree_manager.write_claude_md(
            agent_id="resumed-agent",
            persona_content="# Test Agent\n\nYou are a test agent.",
            project_name="Test Project",
            project_description="A test",
            assignment="Build feature",
            session_state=session_state
        )

        content = path.read_text()

        # Check Session State section is present
        assert "## Session State (from previous session)" in content
        assert "NavBar component complete, tests passing" in content
        assert "src/Nav.tsx, src/Nav.test.tsx" in content
        assert "Wire up routing integration" in content
        assert "Used React Router v6 over v5" in content
        # Blockers is None, so shouldn't appear
        assert "Blockers:" not in content

    def test_write_claude_md_without_session_state(self, worktree_manager):
        """write_claude_md omits session state section when not provided."""
        worktree_manager.create("fresh-agent")

        path = worktree_manager.write_claude_md(
            agent_id="fresh-agent",
            persona_content="# Test Agent\n\nYou are a test agent.",
            project_name="Test Project",
            project_description="A test",
            assignment="Build feature"
        )

        content = path.read_text()

        # Session State section should NOT be present
        assert "## Session State" not in content

    def test_write_claude_md_session_state_with_blockers(self, worktree_manager):
        """write_claude_md includes blockers when present."""
        worktree_manager.create("blocked-agent")

        session_state = {
            "progress": "Started navbar",
            "files_modified": ["src/Nav.tsx"],
            "next_steps": "Need API endpoint",
            "blockers": "Waiting for backend API",
            "decisions": []
        }

        path = worktree_manager.write_claude_md(
            agent_id="blocked-agent",
            persona_content="# Test",
            project_name="Test",
            project_description="Test",
            assignment="Build",
            session_state=session_state
        )

        content = path.read_text()
        assert "Waiting for backend API" in content


class TestWorktreeRemove:
    """Tests for worktree removal."""

    def test_remove_worktree(self, worktree_manager):
        """remove() removes the worktree."""
        path = worktree_manager.create("to-remove")
        assert path.exists()

        result = worktree_manager.remove("to-remove")

        assert result is True
        assert not path.exists()

    def test_remove_nonexistent_worktree(self, worktree_manager):
        """remove() returns False for nonexistent worktree."""
        result = worktree_manager.remove("nonexistent")
        assert result is False

    def test_remove_worktree_with_changes(self, worktree_manager):
        """remove() with force=True removes worktree with uncommitted changes."""
        path = worktree_manager.create("with-changes")

        # Make uncommitted changes
        (path / "new_file.txt").write_text("uncommitted")

        result = worktree_manager.remove("with-changes", force=True)
        assert result is True
        assert not path.exists()


class TestWorktreeList:
    """Tests for listing worktrees."""

    def test_list_worktrees_empty(self, worktree_manager):
        """list_worktrees returns empty list when no worktrees."""
        result = worktree_manager.list_worktrees()
        assert result == []

    def test_list_worktrees(self, worktree_manager):
        """list_worktrees returns all worktrees."""
        worktree_manager.create("agent-1")
        worktree_manager.create("agent-2")

        result = worktree_manager.list_worktrees()

        assert len(result) == 2
        agent_ids = {wt["agent_id"] for wt in result}
        assert agent_ids == {"agent-1", "agent-2"}

    def test_list_worktrees_includes_branch(self, worktree_manager):
        """list_worktrees includes branch name."""
        worktree_manager.create("test-agent")

        result = worktree_manager.list_worktrees()

        assert result[0]["branch"] == "agent/test-agent"


class TestWorktreeExists:
    """Tests for checking worktree existence."""

    def test_exists_true(self, worktree_manager):
        """exists() returns True for existing worktree."""
        worktree_manager.create("existing")
        assert worktree_manager.exists("existing") is True

    def test_exists_false(self, worktree_manager):
        """exists() returns False for nonexistent worktree."""
        assert worktree_manager.exists("nonexistent") is False

    def test_get_worktree_path_exists(self, worktree_manager):
        """get_worktree_path returns path for existing worktree."""
        worktree_manager.create("test")
        path = worktree_manager.get_worktree_path("test")

        assert path is not None
        assert path.exists()

    def test_get_worktree_path_not_exists(self, worktree_manager):
        """get_worktree_path returns None for nonexistent worktree."""
        path = worktree_manager.get_worktree_path("nonexistent")
        assert path is None


class TestWorktreeMerge:
    """Tests for merging worktree branches."""

    def test_merge_worktree(self, worktree_manager):
        """merge() merges agent branch into target."""
        path = worktree_manager.create("merge-test")

        # Make a commit in the worktree
        test_file = path / "feature.txt"
        test_file.write_text("new feature")
        subprocess.run(
            ["git", "add", "feature.txt"],
            cwd=path,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Add feature"],
            cwd=path,
            check=True,
            capture_output=True
        )

        # Merge back to main
        result = worktree_manager.merge("merge-test", summary="Added feature")

        assert result is True

        # Verify file exists in main
        main_file = worktree_manager.repo_path / "feature.txt"
        assert main_file.exists()

    def test_merge_uses_no_ff(self, worktree_manager):
        """merge() uses --no-ff flag."""
        path = worktree_manager.create("noff-test")

        # Make a commit
        (path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add file"], cwd=path, check=True, capture_output=True)

        worktree_manager.merge("noff-test")

        # Check that a merge commit was created (not fast-forward)
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=worktree_manager.repo_path,
            capture_output=True,
            text=True
        )
        assert "Merge" in result.stdout

    def test_merge_nonexistent_worktree(self, worktree_manager):
        """merge() fails for nonexistent worktree."""
        with pytest.raises(WorktreeError, match="does not exist"):
            worktree_manager.merge("nonexistent")


class TestWorktreeBranchStatus:
    """Tests for branch status checking."""

    def test_get_branch_status_clean(self, worktree_manager):
        """get_branch_status returns status for clean worktree."""
        path = worktree_manager.create("status-test")

        status = worktree_manager.get_branch_status("status-test")

        assert status["has_uncommitted"] is False
        assert status["ahead"] == 0
        assert status["behind"] == 0

    def test_get_branch_status_uncommitted(self, worktree_manager):
        """get_branch_status detects uncommitted changes."""
        path = worktree_manager.create("uncommitted-test")

        # Make uncommitted change
        (path / "uncommitted.txt").write_text("uncommitted")

        status = worktree_manager.get_branch_status("uncommitted-test")
        assert status["has_uncommitted"] is True

    def test_get_branch_status_ahead(self, worktree_manager):
        """get_branch_status detects commits ahead of main."""
        path = worktree_manager.create("ahead-test")

        # Make a commit
        (path / "ahead.txt").write_text("ahead")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Ahead"], cwd=path, check=True, capture_output=True)

        status = worktree_manager.get_branch_status("ahead-test")
        assert status["ahead"] == 1


class TestWorktreeAutoCommit:
    """Tests for auto-committing uncommitted changes before teardown."""

    def test_auto_commit_with_uncommitted_files(self, worktree_manager):
        """auto_commit commits untracked and modified files."""
        path = worktree_manager.create("autocommit-test")

        # Create untracked files (simulating agent work that wasn't committed)
        (path / "index.html").write_text("<h1>Hello</h1>")
        (path / "style.css").write_text("body { margin: 0; }")

        committed = worktree_manager.auto_commit("autocommit-test")
        assert committed is True

        # Verify files are committed
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True
        )
        assert result.stdout.strip() == ""  # Clean after commit

        # Verify the commit exists
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=path, capture_output=True, text=True
        )
        assert "Auto-commit" in log.stdout

    def test_auto_commit_clean_worktree(self, worktree_manager):
        """auto_commit returns False for clean worktree."""
        worktree_manager.create("clean-test")
        committed = worktree_manager.auto_commit("clean-test")
        assert committed is False

    def test_auto_commit_nonexistent_worktree(self, worktree_manager):
        """auto_commit returns False for nonexistent worktree."""
        committed = worktree_manager.auto_commit("does-not-exist")
        assert committed is False

    def test_auto_commit_preserves_files_for_merge(self, worktree_manager):
        """auto_commit + merge preserves all agent work on main."""
        path = worktree_manager.create("preserve-test")

        # Agent creates files but doesn't commit
        (path / "app.js").write_text("console.log('hello');")
        (path / "index.html").write_text("<h1>App</h1>")

        # Auto-commit (what teardown should do)
        worktree_manager.auto_commit("preserve-test")

        # Merge to main (what teardown does after auto-commit)
        worktree_manager.merge("preserve-test", "main")

        # Verify files exist on main
        repo_path = worktree_manager.repo_path
        assert (repo_path / "app.js").exists()
        assert (repo_path / "index.html").exists()
        assert (repo_path / "app.js").read_text() == "console.log('hello');"

    def test_auto_commit_with_staged_changes(self, worktree_manager):
        """auto_commit handles already-staged files."""
        path = worktree_manager.create("staged-test")

        # Stage a file but don't commit
        (path / "staged.txt").write_text("staged content")
        subprocess.run(["git", "add", "staged.txt"], cwd=path, check=True, capture_output=True)

        committed = worktree_manager.auto_commit("staged-test")
        assert committed is True


class TestWorktreeCleanup:
    """Tests for bulk cleanup."""

    def test_cleanup_all(self, worktree_manager):
        """cleanup_all removes all worktrees."""
        worktree_manager.create("cleanup-1")
        worktree_manager.create("cleanup-2")
        worktree_manager.create("cleanup-3")

        removed = worktree_manager.cleanup_all()

        assert removed == 3
        assert len(worktree_manager.list_worktrees()) == 0


# --- Fixtures ---

@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository with an initial commit."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True
    )

    # Create initial commit (required for worktrees)
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True
    )

    # Ensure we're on main branch
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True
    )

    return repo_path


@pytest.fixture
def worktree_manager(git_repo):
    """Create a WorktreeManager for the test repo."""
    return WorktreeManager(git_repo)


class TestWorktreeNamespacing:
    """Tests for multi-instance worktree namespacing."""

    def test_namespaced_worktree_path(self, git_repo):
        """Instance ID creates namespaced worktree directory."""
        mgr = WorktreeManager(git_repo, instance_id="abc123")
        path = mgr.create("frontend-1")
        assert ".worktrees/abc123/frontend-1" in str(path)
        assert path.exists()

    def test_namespaced_branch_name(self, git_repo):
        """Instance ID prefixes branch names."""
        mgr = WorktreeManager(git_repo, instance_id="abc123")
        mgr.create("frontend-1")
        branch = mgr._branch_name("frontend-1")
        assert branch == "abc123/agent/frontend-1"

    def test_no_instance_id_backward_compatible(self, git_repo):
        """No instance_id uses old flat paths."""
        mgr = WorktreeManager(git_repo)
        path = mgr.create("frontend-1")
        assert ".worktrees/frontend-1" in str(path)
        assert ".worktrees/None" not in str(path)
        assert mgr._branch_name("frontend-1") == "agent/frontend-1"

    def test_two_instances_same_repo(self, git_repo):
        """Two instances on same repo don't collide."""
        mgr_a = WorktreeManager(git_repo, instance_id="inst-a")
        mgr_b = WorktreeManager(git_repo, instance_id="inst-b")

        path_a = mgr_a.create("archie")
        path_b = mgr_b.create("archie")

        assert path_a != path_b
        assert path_a.exists()
        assert path_b.exists()
        assert "inst-a" in str(path_a)
        assert "inst-b" in str(path_b)

    def test_cleanup_only_own_instance(self, git_repo):
        """cleanup_all only removes own instance's worktrees."""
        mgr_a = WorktreeManager(git_repo, instance_id="inst-a")
        mgr_b = WorktreeManager(git_repo, instance_id="inst-b")

        mgr_a.create("worker-1")
        mgr_b.create("worker-1")

        # Clean up instance A
        removed = mgr_a.cleanup_all()
        assert removed == 1

        # Instance B's worktree still exists
        assert mgr_b.exists("worker-1")

    def test_merge_with_namespaced_branch(self, git_repo):
        """Merge works with namespaced branch names."""
        mgr = WorktreeManager(git_repo, instance_id="inst-x")
        path = mgr.create("dev-1")

        # Create a file and commit in the worktree
        (path / "output.txt").write_text("hello from dev-1")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add output"], cwd=path, check=True, capture_output=True)

        # Merge to main
        mgr.merge("dev-1", "main")

        # Verify file on main
        assert (git_repo / "output.txt").exists()
        assert (git_repo / "output.txt").read_text() == "hello from dev-1"


class TestSetupAgentSkills:
    """Tests for skill injection into agent worktrees."""

    def test_copies_skill_directories(self, worktree_manager):
        """setup_agent_skills copies skill directories into .claude/skills/."""
        worktree_manager.create("eng-1")

        skills_src = worktree_manager.repo_path / "test-skills"
        skill1 = skills_src / "build-engine"
        skill1.mkdir(parents=True)
        (skill1 / "SKILL.md").write_text("---\nname: build-engine\n---\nBuild it.")
        (skill1 / "template.md").write_text("Template content")

        result = worktree_manager.setup_agent_skills("eng-1", skills_src)

        assert result == ["build-engine"]
        target = worktree_manager._worktree_path("eng-1") / ".claude" / "skills" / "build-engine"
        assert target.exists()
        assert (target / "SKILL.md").read_text() == "---\nname: build-engine\n---\nBuild it."
        assert (target / "template.md").read_text() == "Template content"

    def test_skips_dirs_without_skill_md(self, worktree_manager):
        """setup_agent_skills skips directories that don't contain SKILL.md."""
        worktree_manager.create("eng-1")

        skills_src = worktree_manager.repo_path / "test-skills"
        (skills_src / "not-a-skill").mkdir(parents=True)
        (skills_src / "not-a-skill" / "README.md").write_text("Not a skill")

        result = worktree_manager.setup_agent_skills("eng-1", skills_src)
        assert result == []

    def test_returns_empty_for_missing_dir(self, worktree_manager):
        """setup_agent_skills returns empty list for nonexistent source dir."""
        worktree_manager.create("eng-1")
        nonexistent = worktree_manager.repo_path / "no-such-dir"
        result = worktree_manager.setup_agent_skills("eng-1", nonexistent)
        assert result == []

    def test_raises_without_worktree(self, worktree_manager):
        """setup_agent_skills raises WorktreeError if worktree doesn't exist."""
        skills_src = worktree_manager.repo_path / "test-skills"
        skills_src.mkdir()
        with pytest.raises(WorktreeError):
            worktree_manager.setup_agent_skills("nonexistent", skills_src)

    def test_multiple_skills(self, worktree_manager):
        """setup_agent_skills handles multiple skill directories."""
        worktree_manager.create("eng-1")

        skills_src = worktree_manager.repo_path / "test-skills"
        for name in ["alpha", "beta", "gamma"]:
            d = skills_src / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

        result = worktree_manager.setup_agent_skills("eng-1", skills_src)
        assert result == ["alpha", "beta", "gamma"]

        target_base = worktree_manager._worktree_path("eng-1") / ".claude" / "skills"
        for name in ["alpha", "beta", "gamma"]:
            assert (target_base / name / "SKILL.md").exists()

    def test_overwrites_existing_skills(self, worktree_manager):
        """setup_agent_skills overwrites previously copied skills."""
        worktree_manager.create("eng-1")

        skills_src = worktree_manager.repo_path / "test-skills"
        skill = skills_src / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("version 1")

        worktree_manager.setup_agent_skills("eng-1", skills_src)

        # Update source and re-inject
        (skill / "SKILL.md").write_text("version 2")
        worktree_manager.setup_agent_skills("eng-1", skills_src)

        target = worktree_manager._worktree_path("eng-1") / ".claude" / "skills" / "deploy" / "SKILL.md"
        assert target.read_text() == "version 2"
