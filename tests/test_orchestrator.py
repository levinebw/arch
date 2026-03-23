"""Unit tests for ARCH Orchestrator."""

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import yaml

from arch.orchestrator import (
    AgentPoolEntry,
    ArchConfig,
    ArchieConfig,
    GitHubConfig,
    GitHubLabel,
    Orchestrator,
    PermissionsConfig,
    ProjectConfig,
    SandboxConfig,
    SettingsConfig,
    check_container_gate,
    check_github_gate,
    check_permission_gate,
    parse_config,
)


class TestParseConfig:
    """Tests for config parsing."""

    def test_parse_minimal_config(self, tmp_path):
        """parse_config handles minimal valid config."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {
                "name": "Test Project"
            }
        }))

        config = parse_config(config_path)

        assert config.project.name == "Test Project"
        assert config.project.repo == "."
        assert config.archie.model == "claude-opus-4-6"
        assert config.settings.mcp_port == 3999

    def test_parse_full_config(self, tmp_path):
        """parse_config handles complete config."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {
                "name": "Full Project",
                "description": "A complete project",
                "repo": "/path/to/repo"
            },
            "archie": {
                "persona": "custom/archie.md",
                "model": "claude-opus-4-5"
            },
            "agent_pool": [
                {
                    "id": "frontend-dev",
                    "persona": "personas/frontend.md",
                    "model": "claude-sonnet-4-6",
                    "max_instances": 2,
                    "sandbox": {
                        "enabled": True,
                        "image": "custom:latest",
                        "memory_limit": "2g"
                    },
                    "permissions": {
                        "skip_permissions": True
                    }
                }
            ],
            "github": {
                "repo": "owner/repo",
                "default_branch": "develop",
                "labels": [
                    {"name": "agent:frontend", "color": "ff0000"}
                ]
            },
            "settings": {
                "max_concurrent_agents": 10,
                "mcp_port": 4000,
                "token_budget_usd": 50.0
            }
        }))

        config = parse_config(config_path)

        assert config.project.name == "Full Project"
        assert config.project.description == "A complete project"
        assert config.archie.persona == "custom/archie.md"
        assert len(config.agent_pool) == 1
        assert config.agent_pool[0].id == "frontend-dev"
        assert config.agent_pool[0].sandbox.enabled is True
        assert config.agent_pool[0].sandbox.memory_limit == "2g"
        assert config.agent_pool[0].permissions.skip_permissions is True
        assert config.github.repo == "owner/repo"
        assert config.github.default_branch == "develop"
        assert len(config.github.labels) == 1
        assert config.settings.mcp_port == 4000
        assert config.settings.token_budget_usd == 50.0

    def test_parse_config_file_not_found(self, tmp_path):
        """parse_config raises on missing file."""
        with pytest.raises(FileNotFoundError):
            parse_config(tmp_path / "nonexistent.yaml")

    def test_parse_config_empty_file(self, tmp_path):
        """parse_config raises on empty file."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text("")

        with pytest.raises(ValueError, match="empty"):
            parse_config(config_path)

    def test_parse_config_missing_project(self, tmp_path):
        """parse_config raises on missing project section."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({"settings": {}}))

        with pytest.raises(ValueError, match="project"):
            parse_config(config_path)

    def test_parse_config_missing_project_name(self, tmp_path):
        """parse_config raises on missing project name."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({"project": {"description": "test"}}))

        with pytest.raises(ValueError, match="name"):
            parse_config(config_path)

    def test_parse_config_agent_missing_id(self, tmp_path):
        """parse_config raises on agent without id."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {"name": "Test"},
            "agent_pool": [{"persona": "test.md"}]
        }))

        with pytest.raises(ValueError, match="id"):
            parse_config(config_path)

    def test_parse_config_agent_missing_persona(self, tmp_path):
        """parse_config raises on agent without persona."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {"name": "Test"},
            "agent_pool": [{"id": "test-agent"}]
        }))

        with pytest.raises(ValueError, match="persona"):
            parse_config(config_path)


class TestGateChecks:
    """Tests for startup gate checks."""

    def test_check_permission_gate_empty(self):
        """check_permission_gate returns empty for no skip_permissions."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(id="agent1", persona="p1.md"),
                AgentPoolEntry(id="agent2", persona="p2.md"),
            ]
        )

        result = check_permission_gate(config)
        assert result == []

    def test_check_permission_gate_with_agents(self):
        """check_permission_gate returns agents with skip_permissions."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(id="safe", persona="p1.md"),
                AgentPoolEntry(
                    id="dangerous",
                    persona="p2.md",
                    permissions=PermissionsConfig(skip_permissions=True)
                ),
            ]
        )

        result = check_permission_gate(config)
        assert result == ["dangerous"]

    def test_check_container_gate_no_containers(self):
        """check_container_gate succeeds when no containers needed."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(id="agent1", persona="p1.md"),
            ]
        )

        ok, agents, missing = check_container_gate(config)
        assert ok is True
        assert agents == []
        assert missing == []

    def test_check_container_gate_docker_unavailable(self):
        """check_container_gate fails when Docker unavailable."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(
                    id="sandboxed",
                    persona="p1.md",
                    sandbox=SandboxConfig(enabled=True)
                ),
            ]
        )

        with patch("arch.orchestrator.check_docker_available", return_value=(False, "Not available")):
            ok, agents, missing = check_container_gate(config)

        assert ok is False
        assert agents == ["sandboxed"]

    def test_check_container_gate_image_exists(self):
        """check_container_gate succeeds when image exists."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(
                    id="sandboxed",
                    persona="p1.md",
                    sandbox=SandboxConfig(enabled=True, image="test:latest")
                ),
            ]
        )

        with patch("arch.orchestrator.check_docker_available", return_value=(True, "OK")):
            with patch("arch.orchestrator.check_image_exists", return_value=True):
                ok, agents, missing = check_container_gate(config)

        assert ok is True
        assert agents == ["sandboxed"]
        assert missing == []

    def test_check_container_gate_image_missing(self):
        """check_container_gate identifies missing images."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            agent_pool=[
                AgentPoolEntry(
                    id="sandboxed",
                    persona="p1.md",
                    sandbox=SandboxConfig(enabled=True, image="missing:latest")
                ),
            ]
        )

        with patch("arch.orchestrator.check_docker_available", return_value=(True, "OK")):
            with patch("arch.orchestrator.check_image_exists", return_value=False):
                ok, agents, missing = check_container_gate(config)

        assert ok is True
        assert missing == ["missing:latest"]

    def test_check_github_gate_not_configured(self):
        """check_github_gate succeeds when not configured."""
        config = ArchConfig(project=ProjectConfig(name="Test"))

        ok, msg = check_github_gate(config)
        assert ok is True
        assert "not configured" in msg.lower()

    def test_check_github_gate_gh_not_found(self):
        """check_github_gate fails when gh not installed."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            github=GitHubConfig(repo="owner/repo")
        )

        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, msg = check_github_gate(config)

        assert ok is False
        assert "not installed" in msg.lower()

    def test_check_github_gate_success(self):
        """check_github_gate succeeds with valid auth."""
        config = ArchConfig(
            project=ProjectConfig(name="Test"),
            github=GitHubConfig(repo="owner/repo")
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")
            ok, msg = check_github_gate(config)

        assert ok is True
        assert "owner/repo" in msg


class TestOrchestratorInit:
    """Tests for Orchestrator initialization."""

    def test_init_defaults(self, tmp_path):
        """Orchestrator initializes with defaults."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({"project": {"name": "Test"}}))

        orch = Orchestrator(config_path)

        assert orch.config_path == config_path
        assert orch.keep_worktrees is False
        assert orch._running is False

    def test_init_keep_worktrees(self, tmp_path):
        """Orchestrator respects keep_worktrees flag."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({"project": {"name": "Test"}}))

        orch = Orchestrator(config_path, keep_worktrees=True)

        assert orch.keep_worktrees is True


class TestOrchestratorStartup:
    """Tests for Orchestrator startup sequence."""

    @pytest.mark.asyncio
    async def test_startup_parses_config(self, orchestrator, mock_all_gates):
        """startup() parses config file."""
        await orchestrator.startup()

        assert orchestrator.config is not None
        assert orchestrator.config.project.name == "Test Project"

    @pytest.mark.asyncio
    async def test_startup_initializes_state(self, orchestrator, mock_all_gates):
        """startup() initializes state store."""
        await orchestrator.startup()

        assert orchestrator.state is not None
        project = orchestrator.state.get_project()
        assert project["name"] == "Test Project"

    @pytest.mark.asyncio
    async def test_startup_initializes_token_tracker(self, orchestrator, mock_all_gates):
        """startup() initializes token tracker."""
        await orchestrator.startup()

        assert orchestrator.token_tracker is not None

    @pytest.mark.asyncio
    async def test_startup_checks_permission_gate(self, orchestrator, tmp_path):
        """startup() runs permission gate check."""
        # Config with skip_permissions
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {"name": "Test"},
            "agent_pool": [{
                "id": "dangerous",
                "persona": "p.md",
                "permissions": {"skip_permissions": True}
            }]
        }))

        orch = Orchestrator(config_path)

        with patch("builtins.input", return_value="n"):
            with patch_git_and_session():
                result = await orch.startup()

        # Should fail because user declined
        assert result is False

    @pytest.mark.asyncio
    async def test_startup_spawns_archie(self, orchestrator, mock_all_gates):
        """startup() spawns Archie session."""
        await orchestrator.startup()

        assert orchestrator._archie_session is not None
        assert orchestrator.session_manager is not None


class TestOrchestratorShutdown:
    """Tests for Orchestrator shutdown sequence."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_sessions(self, orchestrator, mock_all_gates):
        """shutdown() stops all sessions."""
        await orchestrator.startup()

        # Mock stop_all
        orchestrator.session_manager.stop_all = AsyncMock(return_value=1)

        await orchestrator.shutdown()

        orchestrator.session_manager.stop_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_removes_worktrees(self, orchestrator, mock_all_gates):
        """shutdown() removes worktrees when keep_worktrees=False."""
        await orchestrator.startup()

        # Mock cleanup
        orchestrator.worktree_manager.cleanup_all = Mock(return_value=1)

        await orchestrator.shutdown(keep_worktrees=False)

        orchestrator.worktree_manager.cleanup_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_keeps_worktrees_when_requested(self, orchestrator, mock_all_gates):
        """shutdown() keeps worktrees when keep_worktrees=True."""
        await orchestrator.startup()

        orchestrator.worktree_manager.cleanup_all = Mock()

        await orchestrator.shutdown(keep_worktrees=True)

        orchestrator.worktree_manager.cleanup_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_logs_state_persisted(self, orchestrator, mock_all_gates, caplog):
        """shutdown() logs that state is persisted."""
        await orchestrator.startup()
        await orchestrator.shutdown()

        # StateStore auto-persists, so shutdown just logs confirmation
        assert "persisted" in caplog.text.lower() or orchestrator.state is not None


class TestOrchestratorSignals:
    """Tests for signal handling."""

    @pytest.mark.asyncio
    async def test_signal_handler_sets_shutdown(self, orchestrator, mock_all_gates):
        """Signal handler sets shutdown flag."""
        await orchestrator.startup()

        orchestrator._signal_handler(signal.SIGINT, None)

        assert orchestrator._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_registers_signal_handlers(self, orchestrator, mock_all_gates):
        """startup() registers signal handlers."""
        with patch("signal.signal") as mock_signal:
            await orchestrator.startup()

        # Should register SIGINT and SIGTERM
        calls = [c[0][0] for c in mock_signal.call_args_list]
        assert signal.SIGINT in calls
        assert signal.SIGTERM in calls


class TestOrchestratorArchieRestart:
    """Tests for Archie restart logic."""

    @pytest.mark.asyncio
    async def test_archie_restart_with_session_id(self, orchestrator, mock_all_gates):
        """Archie crash restart uses session ID for resume."""
        await orchestrator.startup()

        # Simulate Archie crash (non-zero exit code)
        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 1
        orchestrator._archie_session._session_id = "test-session-id"

        # Mock spawn for restart
        new_session = MagicMock()
        new_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=new_session)

        await orchestrator._handle_archie_exit()

        # Should attempt spawn with resume
        orchestrator.session_manager.spawn.assert_called_once()
        call_args = orchestrator.session_manager.spawn.call_args
        assert call_args[1]["resume_session_id"] == "test-session-id"

        # Config should include allowed_tools
        config = call_args[0][0]
        assert len(config.allowed_tools) > 0
        assert config.permission_prompt_tool == "mcp__arch__handle_permission_request"

    @pytest.mark.asyncio
    async def test_archie_normal_exit_auto_resumes(self, orchestrator, mock_all_gates):
        """Normal Archie exit (code 0) auto-resumes without asking user."""
        await orchestrator.startup()

        # Simulate normal exit
        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 0

        mock_session = MagicMock()
        mock_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=mock_session)
        orchestrator.mcp_server._escalate_and_wait = AsyncMock()

        await orchestrator._handle_archie_exit()

        # Should auto-resume without escalating
        orchestrator.session_manager.spawn.assert_called_once()
        orchestrator.mcp_server._escalate_and_wait.assert_not_called()
        assert orchestrator._shutdown_requested is False
        assert orchestrator._crash_restart_count == 0

    @pytest.mark.asyncio
    async def test_archie_normal_exit_no_restart_when_disabled(self, orchestrator, mock_all_gates):
        """Normal Archie exit escalates to user when auto_resume_archie=False."""
        await orchestrator.startup()
        orchestrator.config.settings.auto_resume_archie = False

        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 0

        orchestrator.session_manager.spawn = AsyncMock()
        orchestrator.mcp_server._escalate_and_wait = AsyncMock(return_value="Shut down")

        await orchestrator._handle_archie_exit()

        # Should NOT attempt restart (user chose shutdown)
        orchestrator.session_manager.spawn.assert_not_called()
        assert orchestrator._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_archie_normal_exit_resume_when_disabled(self, orchestrator, mock_all_gates):
        """Normal Archie exit with user choosing Resume when auto_resume disabled."""
        await orchestrator.startup()
        orchestrator.config.settings.auto_resume_archie = False

        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 0

        mock_session = MagicMock()
        mock_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=mock_session)
        orchestrator.mcp_server._escalate_and_wait = AsyncMock(return_value="Resume Archie")

        await orchestrator._handle_archie_exit()

        # Should resume Archie
        orchestrator.session_manager.spawn.assert_called_once()
        assert orchestrator._shutdown_requested is False

    @pytest.mark.asyncio
    async def test_archie_normal_exit_project_complete_no_escalation(self, orchestrator, mock_all_gates):
        """Normal exit after close_project skips escalation."""
        await orchestrator.startup()

        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 0
        orchestrator._project_complete = True

        orchestrator.mcp_server._escalate_and_wait = AsyncMock()

        await orchestrator._handle_archie_exit()

        # Should NOT escalate — project already closed properly
        orchestrator.mcp_server._escalate_and_wait.assert_not_called()
        assert orchestrator._shutdown_requested is False

    @pytest.mark.asyncio
    async def test_archie_crash_restart_limit(self, orchestrator, mock_all_gates):
        """Archie shutdown after multiple crash restarts."""
        await orchestrator.startup()

        orchestrator._crash_restart_count = 2
        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 1  # Crash

        await orchestrator._handle_archie_exit()

        assert orchestrator._shutdown_requested is True


class TestCostSummary:
    """Tests for cost summary output."""

    @pytest.mark.asyncio
    async def test_cost_summary_output(self, orchestrator, mock_all_gates, capsys):
        """shutdown() prints cost summary."""
        await orchestrator.startup()

        # Add some usage via parse_stream_event (expects JSON string)
        orchestrator.token_tracker.register_agent("archie", "claude-opus-4-5")
        usage_line = json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0
            }}
        })
        orchestrator.token_tracker.parse_stream_event("archie", usage_line)

        await orchestrator.shutdown()

        captured = capsys.readouterr()
        assert "COST SUMMARY" in captured.out
        assert "archie" in captured.out


class TestAgentLifecycleHandlers:
    """Tests for agent lifecycle handlers (spawn, teardown, merge)."""

    @pytest.mark.asyncio
    async def test_spawn_agent_creates_session(self, orchestrator_with_pool, mock_all_gates):
        """spawn_agent creates worktree and session."""
        await orchestrator_with_pool.startup()

        result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build the navbar component"
        )

        assert "agent_id" in result
        assert result["agent_id"].startswith("test-agent-")
        assert "worktree_path" in result
        assert result["status"] == "spawning"

    @pytest.mark.asyncio
    async def test_spawn_agent_unknown_role(self, orchestrator_with_pool, mock_all_gates):
        """spawn_agent rejects unknown roles."""
        await orchestrator_with_pool.startup()

        result = await orchestrator_with_pool._handle_spawn_agent(
            role="nonexistent-role",
            assignment="Do something"
        )

        assert "error" in result
        assert "Unknown role" in result["error"]

    @pytest.mark.asyncio
    async def test_spawn_agent_max_instances(self, orchestrator_with_pool, mock_all_gates):
        """spawn_agent respects max_instances limit."""
        await orchestrator_with_pool.startup()

        # Spawn first instance (max is 1)
        result1 = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="First task"
        )
        assert "agent_id" in result1

        # Try to spawn second instance
        result2 = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Second task"
        )
        assert "error" in result2
        assert "Max instances" in result2["error"]

    @pytest.mark.asyncio
    async def test_spawn_agent_sandboxed(self, orchestrator_with_sandbox, mock_all_gates):
        """spawn_agent creates containerized session for sandboxed role."""
        await orchestrator_with_sandbox.startup()

        result = await orchestrator_with_sandbox._handle_spawn_agent(
            role="sandboxed-agent",
            assignment="Secure task"
        )

        assert "agent_id" in result
        assert result["sandboxed"] is True

    @pytest.mark.asyncio
    async def test_teardown_agent(self, orchestrator_with_pool, mock_all_gates):
        """teardown_agent stops session and cleans up."""
        await orchestrator_with_pool.startup()

        # Spawn an agent first
        spawn_result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build something"
        )
        agent_id = spawn_result["agent_id"]

        # Teardown the agent
        result = await orchestrator_with_pool._handle_teardown_agent(agent_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_teardown_archie_rejected(self, orchestrator_with_pool, mock_all_gates):
        """teardown_agent rejects tearing down Archie."""
        await orchestrator_with_pool.startup()

        result = await orchestrator_with_pool._handle_teardown_agent("archie")

        assert result is False

    @pytest.mark.asyncio
    async def test_request_merge_direct(self, orchestrator_with_pool, mock_all_gates):
        """request_merge performs direct merge."""
        await orchestrator_with_pool.startup()

        # Spawn an agent first
        spawn_result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build something"
        )
        agent_id = spawn_result["agent_id"]

        # Mock the merge to succeed
        orchestrator_with_pool.worktree_manager.merge = Mock(return_value=True)

        result = await orchestrator_with_pool._handle_request_merge(
            agent_id=agent_id,
            target_branch="main"
        )

        assert result["status"] == "approved"

    @pytest.mark.asyncio
    async def test_request_merge_creates_pr(self, orchestrator_with_pool, mock_all_gates):
        """request_merge creates PR when title provided."""
        await orchestrator_with_pool.startup()

        # Spawn an agent first
        spawn_result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build something"
        )
        agent_id = spawn_result["agent_id"]

        # Mock PR creation
        orchestrator_with_pool.worktree_manager.create_pr = Mock(
            return_value="https://github.com/owner/repo/pull/123"
        )

        result = await orchestrator_with_pool._handle_request_merge(
            agent_id=agent_id,
            target_branch="main",
            pr_title="Add navbar component",
            pr_body="Implements the navbar as specified"
        )

        assert result["status"] == "approved"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/123"

    @pytest.mark.asyncio
    async def test_close_project_marks_complete(self, orchestrator_with_pool, mock_all_gates):
        """close_project marks project complete and adds summary to state."""
        await orchestrator_with_pool.startup()

        result = await orchestrator_with_pool._handle_close_project("Work complete")

        assert result is True
        assert orchestrator_with_pool._project_complete is True
        project = orchestrator_with_pool.state.get_project()
        assert project["status"] == "complete"
        assert project["summary"] == "Work complete"

    @pytest.mark.asyncio
    async def test_spawn_decrements_on_exit(self, orchestrator_with_pool, mock_all_gates):
        """Instance count decrements when agent exits."""
        await orchestrator_with_pool.startup()

        # Spawn an agent
        result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build something"
        )
        agent_id = result["agent_id"]

        assert orchestrator_with_pool._agent_instance_counts.get("test-agent", 0) == 1

        # Simulate agent exit
        await orchestrator_with_pool._on_agent_exit(agent_id, 0)

        assert orchestrator_with_pool._agent_instance_counts.get("test-agent", 0) == 0


class TestArchieAutoResume:
    """Tests for Archie auto-resume on unread messages (Issue #2)."""

    @pytest.mark.asyncio
    async def test_auto_resume_triggered_with_unread_messages(self, orchestrator, mock_all_gates):
        """Auto-resume triggers when Archie has unread messages after cooldown."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit
        orchestrator._archie_session._running = False
        orchestrator._archie_session._session_id = "test-session"
        orchestrator._archie_last_exit_time = time.time() - 15  # Past cooldown

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Please check this")

        # Mock spawn for resume
        new_session = MagicMock()
        new_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=new_session)

        await orchestrator._check_auto_resume()

        # Should have called spawn to resume
        orchestrator.session_manager.spawn.assert_called_once()
        call_kwargs = orchestrator.session_manager.spawn.call_args[1]
        assert call_kwargs["resume_session_id"] == "test-session"

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_archie_running(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger when Archie is still running."""
        await orchestrator.startup()

        # Archie is running
        assert orchestrator._archie_session.is_running

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Hello")

        # Mock spawn to track if called
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_shutdown_requested(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger when shutdown is requested."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit and shutdown requested
        orchestrator._archie_session._running = False
        orchestrator._archie_last_exit_time = time.time() - 15
        orchestrator._shutdown_requested = True

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Hello")

        # Mock spawn
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_within_cooldown(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger within cooldown period."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit just now (within cooldown)
        orchestrator._archie_session._running = False
        orchestrator._archie_last_exit_time = time.time() - 2  # Only 2 seconds ago

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Hello")

        # Mock spawn
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn (within 10s cooldown)
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_resume_limit_exceeded(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger when message resume limit exceeded."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit with resume limit exceeded
        orchestrator._archie_session._running = False
        orchestrator._archie_last_exit_time = time.time() - 15
        orchestrator._message_resume_count = 51  # Limit is 50

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Hello")

        # Mock spawn
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_no_unread_messages(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger when no unread messages."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit
        orchestrator._archie_session._running = False
        orchestrator._archie_last_exit_time = time.time() - 15

        # No messages added

        # Mock spawn
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_not_triggered_no_exit_time(self, orchestrator, mock_all_gates):
        """Auto-resume does not trigger when Archie never ran."""
        await orchestrator.startup()

        # Simulate Archie not running but no exit time recorded
        orchestrator._archie_session._running = False
        orchestrator._archie_last_exit_time = None

        # Add unread message
        orchestrator.state.add_message("user", "archie", "Hello")

        # Mock spawn
        orchestrator.session_manager.spawn = AsyncMock()

        await orchestrator._check_auto_resume()

        # Should NOT have called spawn
        orchestrator.session_manager.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_for_messages_prompt(self, orchestrator, mock_all_gates):
        """_resume_archie_for_messages uses correct prompt."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit
        orchestrator._archie_session._running = False
        orchestrator._archie_session._session_id = "test-session"
        orchestrator._archie_last_exit_time = time.time() - 15

        # Mock spawn
        new_session = MagicMock()
        new_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=new_session)

        await orchestrator._resume_archie_for_messages()

        # Check prompt content
        call_args = orchestrator.session_manager.spawn.call_args
        prompt = call_args[0][1]  # Second positional arg
        assert "unread messages" in prompt.lower()
        assert "get_messages" in prompt

    @pytest.mark.asyncio
    async def test_resume_for_messages_increments_resume_count(self, orchestrator, mock_all_gates):
        """_resume_archie_for_messages increments message resume count."""
        import time

        await orchestrator.startup()

        # Simulate Archie exit
        orchestrator._archie_session._running = False
        orchestrator._archie_session._session_id = "test-session"
        orchestrator._archie_last_exit_time = time.time() - 15

        initial_count = orchestrator._message_resume_count

        # Mock spawn
        new_session = MagicMock()
        new_session.is_running = True
        orchestrator.session_manager.spawn = AsyncMock(return_value=new_session)

        await orchestrator._resume_archie_for_messages()

        assert orchestrator._message_resume_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_handle_archie_exit_records_exit_time(self, orchestrator, mock_all_gates):
        """_handle_archie_exit records the exit time."""
        import time

        await orchestrator.startup()

        # Initially no exit time
        assert orchestrator._archie_last_exit_time is None

        # Simulate Archie exit (normal, code 0)
        orchestrator._archie_session._running = False
        orchestrator._archie_session._exit_code = 0

        # Mock escalation so it doesn't block
        orchestrator.mcp_server._escalate_and_wait = AsyncMock(return_value="Shut down")

        before = time.time()
        await orchestrator._handle_archie_exit()
        after = time.time()

        # Exit time should be recorded even for normal exits
        assert orchestrator._archie_last_exit_time is not None
        assert before <= orchestrator._archie_last_exit_time <= after


class TestSessionStatePersistence:
    """Tests for agent session state persistence (Step 11.5)."""

    @pytest.mark.asyncio
    async def test_archie_context_injected_on_restart(self, orchestrator, mock_all_gates):
        """Session state is injected into Archie's CLAUDE.md when present."""
        await orchestrator.startup()

        # Simulate Archie having saved progress in a previous session
        orchestrator.state.update_agent("archie", context={
            "progress": "Sprint planning complete",
            "files_modified": ["BRIEF.md"],
            "next_steps": "Spawn frontend agent",
            "decisions": ["Using React for frontend"]
        })

        # Re-create Archie's worktree (simulating restart)
        orchestrator.worktree_manager.remove.reset_mock()
        orchestrator.worktree_manager.write_claude_md.reset_mock()

        await orchestrator._create_archie_worktree()

        # Verify write_claude_md was called with session_state
        orchestrator.worktree_manager.write_claude_md.assert_called_once()
        call_kwargs = orchestrator.worktree_manager.write_claude_md.call_args.kwargs
        assert call_kwargs["session_state"] == {
            "progress": "Sprint planning complete",
            "files_modified": ["BRIEF.md"],
            "next_steps": "Spawn frontend agent",
            "decisions": ["Using React for frontend"]
        }

    @pytest.mark.asyncio
    async def test_archie_no_context_no_session_state(self, orchestrator, mock_all_gates):
        """CLAUDE.md has no Session State section when no context exists."""
        await orchestrator.startup()

        # Verify write_claude_md was called without session_state (or with None)
        call_kwargs = orchestrator.worktree_manager.write_claude_md.call_args.kwargs
        assert call_kwargs.get("session_state") is None

    @pytest.mark.asyncio
    async def test_worker_tools_include_save_progress(self, orchestrator_with_pool, mock_all_gates):
        """Worker agents have save_progress in their tool list."""
        await orchestrator_with_pool.startup()

        result = await orchestrator_with_pool._handle_spawn_agent(
            role="test-agent",
            assignment="Build feature"
        )

        # Verify write_claude_md was called with save_progress in tools
        # (The second call should be for the spawned agent)
        calls = orchestrator_with_pool.worktree_manager.write_claude_md.call_args_list
        # Find the call for the spawned agent (not archie)
        agent_call = [c for c in calls if c.kwargs.get("agent_id", "").startswith("test-agent")]
        assert len(agent_call) == 1
        assert "save_progress" in agent_call[0].kwargs["available_tools"]

    @pytest.mark.asyncio
    async def test_archie_tools_include_save_progress(self, orchestrator, mock_all_gates):
        """Archie has save_progress in tool list."""
        await orchestrator.startup()

        # Verify write_claude_md was called with save_progress for archie
        call_kwargs = orchestrator.worktree_manager.write_claude_md.call_args.kwargs
        assert "save_progress" in call_kwargs["available_tools"]


# ============================================================================
# Helper Functions and Fixtures
# ============================================================================


def create_mock_process(output_lines=None, exit_code=0):
    """Create a mock asyncio subprocess."""
    mock = MagicMock()
    mock.pid = 12345

    if output_lines is None:
        output_lines = []

    output_iter = iter(output_lines + [None])

    async def readline():
        try:
            line = next(output_iter)
            if line is None:
                return b""
            return (line + "\n").encode()
        except StopIteration:
            return b""

    mock.stdout = MagicMock()
    mock.stdout.readline = readline
    mock.stderr = MagicMock()
    mock.wait = AsyncMock(return_value=exit_code)
    mock.terminate = MagicMock()
    mock.kill = MagicMock()

    return mock


def patch_git_and_session():
    """Patch git and session creation for testing."""
    return patch.multiple(
        "arch.orchestrator",
        subprocess=MagicMock(run=MagicMock(return_value=Mock(returncode=0))),
    )


@pytest.fixture
def tmp_config(tmp_path):
    """Create a minimal test config file."""
    config_path = tmp_path / "arch.yaml"
    config_path.write_text(yaml.dump({
        "project": {
            "name": "Test Project",
            "description": "A test project",
            "repo": str(tmp_path)
        },
        "archie": {
            "persona": "personas/archie.md"
        },
        "settings": {
            "state_dir": str(tmp_path / "state"),
            "mcp_port": 3999
        }
    }))

    # Create minimal persona
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "archie.md").write_text("# Archie\nLead agent.")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    return config_path


@pytest.fixture
def orchestrator(tmp_config, tmp_path):
    """Create an Orchestrator for testing."""
    return Orchestrator(tmp_config)


@pytest.fixture
def mock_all_gates(tmp_path):
    """Mock all gates and subprocess calls for testing."""
    from arch.mcp_server import MCPServer
    from arch.worktree import WorktreeManager

    mock_process = create_mock_process()

    # Create mock worktree path
    worktree_path = tmp_path / ".worktrees" / "archie"
    worktree_path.mkdir(parents=True, exist_ok=True)

    # Mock WorktreeManager
    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_worktree_manager.create.return_value = worktree_path
    mock_worktree_manager.get_worktree_path.return_value = worktree_path
    mock_worktree_manager.write_claude_md.return_value = None
    mock_worktree_manager.cleanup_all.return_value = 1

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

            with patch("arch.orchestrator.check_docker_available", return_value=(True, "OK")):
                with patch("arch.orchestrator.check_image_exists", return_value=True):
                    with patch("arch.orchestrator.WorktreeManager", return_value=mock_worktree_manager):
                        with patch.object(MCPServer, "start", new_callable=AsyncMock):
                            with patch.object(MCPServer, "stop", new_callable=AsyncMock):
                                yield


@pytest.fixture
def tmp_config_with_pool(tmp_path):
    """Create a test config with agent pool."""
    config_path = tmp_path / "arch.yaml"
    config_path.write_text(yaml.dump({
        "project": {
            "name": "Test Project",
            "description": "A test project",
            "repo": str(tmp_path)
        },
        "archie": {
            "persona": "personas/archie.md"
        },
        "agent_pool": [
            {
                "id": "test-agent",
                "persona": "personas/test.md",
                "model": "claude-sonnet-4-6",
                "max_instances": 1
            }
        ],
        "settings": {
            "state_dir": str(tmp_path / "state"),
            "mcp_port": 3999,
            "max_concurrent_agents": 5
        }
    }))

    # Create personas
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "archie.md").write_text("# Archie\nLead agent.")
    (personas_dir / "test.md").write_text("# Test Agent\nA test agent.")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    return config_path


@pytest.fixture
def tmp_config_with_sandbox(tmp_path):
    """Create a test config with sandboxed agent."""
    config_path = tmp_path / "arch.yaml"
    config_path.write_text(yaml.dump({
        "project": {
            "name": "Test Project",
            "description": "A test project",
            "repo": str(tmp_path)
        },
        "archie": {
            "persona": "personas/archie.md"
        },
        "agent_pool": [
            {
                "id": "sandboxed-agent",
                "persona": "personas/sandboxed.md",
                "model": "claude-sonnet-4-6",
                "max_instances": 1,
                "sandbox": {
                    "enabled": True,
                    "image": "arch-agent:latest"
                }
            }
        ],
        "settings": {
            "state_dir": str(tmp_path / "state"),
            "mcp_port": 3999,
            "max_concurrent_agents": 5
        }
    }))

    # Create personas
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "archie.md").write_text("# Archie\nLead agent.")
    (personas_dir / "sandboxed.md").write_text("# Sandboxed Agent\nA sandboxed agent.")

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    return config_path


@pytest.fixture
def orchestrator_with_pool(tmp_config_with_pool, tmp_path):
    """Create an Orchestrator with agent pool for testing."""
    return Orchestrator(tmp_config_with_pool)


@pytest.fixture
def orchestrator_with_sandbox(tmp_config_with_sandbox, tmp_path):
    """Create an Orchestrator with sandboxed agent for testing."""
    return Orchestrator(tmp_config_with_sandbox)
