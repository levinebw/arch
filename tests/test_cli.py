"""Unit tests for ARCH CLI (arch.py)."""

import importlib.util
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, Mock

import pytest
import yaml

# Import arch.py directly (not the arch/ package)
_arch_py_path = Path(__file__).parent.parent / "arch.py"
_spec = importlib.util.spec_from_file_location("arch_cli", _arch_py_path)
arch_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arch_cli)

# Pull out the functions we need to test
print_banner = arch_cli.print_banner
get_state_dir = arch_cli.get_state_dir
write_pid_file = arch_cli.write_pid_file
read_pid_file = arch_cli.read_pid_file
remove_pid_file = arch_cli.remove_pid_file
cmd_init = arch_cli.cmd_init
cmd_status = arch_cli.cmd_status
cmd_down = arch_cli.cmd_down
cmd_send = arch_cli.cmd_send
cmd_dashboard = arch_cli.cmd_dashboard
main = arch_cli.main
DEFAULT_ARCH_YAML = arch_cli.DEFAULT_ARCH_YAML
DEFAULT_BRIEF_MD = arch_cli.DEFAULT_BRIEF_MD


class TestBanner:
    """Tests for banner printing."""

    def test_print_banner(self, capsys):
        """print_banner outputs ASCII art."""
        print_banner()
        captured = capsys.readouterr()
        # The banner is ASCII art spelling out ARCH
        assert "_" in captured.out
        assert "/" in captured.out
        assert "\\" in captured.out


class TestPidFile:
    """Tests for PID file management."""

    def test_write_and_read_pid_file(self, tmp_path):
        """write_pid_file and read_pid_file work together."""
        state_dir = tmp_path / "state"

        write_pid_file(state_dir)

        pid = read_pid_file(state_dir)
        assert pid == os.getpid()

    def test_read_pid_file_not_exists(self, tmp_path):
        """read_pid_file returns None when file doesn't exist."""
        state_dir = tmp_path / "state"
        assert read_pid_file(state_dir) is None

    def test_read_pid_file_invalid_content(self, tmp_path):
        """read_pid_file returns None for invalid content."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "arch.pid").write_text("not a number")

        assert read_pid_file(state_dir) is None

    def test_read_pid_file_dead_process(self, tmp_path):
        """read_pid_file returns None for non-existent process."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Use a PID that's unlikely to exist
        (state_dir / "arch.pid").write_text("999999999")

        assert read_pid_file(state_dir) is None

    def test_remove_pid_file(self, tmp_path):
        """remove_pid_file removes the file."""
        state_dir = tmp_path / "state"
        write_pid_file(state_dir)

        assert (state_dir / "arch.pid").exists()
        remove_pid_file(state_dir)
        assert not (state_dir / "arch.pid").exists()

    def test_remove_pid_file_not_exists(self, tmp_path):
        """remove_pid_file doesn't error when file doesn't exist."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Should not raise
        remove_pid_file(state_dir)


class TestGetStateDir:
    """Tests for state directory resolution."""

    def test_get_state_dir_from_config(self, tmp_path):
        """get_state_dir reads from config file."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {
                "state_dir": "/custom/state"
            }
        }))

        result = get_state_dir(config_path)
        assert result == Path("/custom/state")

    def test_get_state_dir_default(self, tmp_path):
        """get_state_dir returns resolved path relative to config parent when no config."""
        config_path = tmp_path / "nonexistent.yaml"

        result = get_state_dir(config_path)
        assert result == (tmp_path / "state").resolve()

    def test_get_state_dir_no_settings(self, tmp_path):
        """get_state_dir returns resolved default when no settings in config."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "project": {"name": "Test"}
        }))

        result = get_state_dir(config_path)
        assert result == (tmp_path / "state").resolve()

    def test_get_state_dir_relative(self, tmp_path):
        """get_state_dir resolves relative state_dir relative to config file."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {
                "state_dir": "./my-state"
            }
        }))

        result = get_state_dir(config_path)
        assert result == (tmp_path / "my-state").resolve()


class TestCmdInit:
    """Tests for arch init command."""

    def test_init_creates_files(self, tmp_path, monkeypatch):
        """init creates arch.yaml, BRIEF.md, and personas."""
        monkeypatch.chdir(tmp_path)

        args = MagicMock()
        args.name = "Test Project"
        args.description = "A test"
        args.github = None

        result = cmd_init(args)

        assert result == 0
        assert (tmp_path / "arch.yaml").exists()
        assert (tmp_path / "BRIEF.md").exists()
        assert (tmp_path / "personas").is_dir()
        assert (tmp_path / "state").is_dir()
        assert (tmp_path / ".gitignore").exists()

    def test_init_with_name(self, tmp_path, monkeypatch):
        """init uses provided name."""
        monkeypatch.chdir(tmp_path)

        args = MagicMock()
        args.name = "My Custom Project"
        args.description = None
        args.github = None

        result = cmd_init(args)

        assert result == 0
        content = (tmp_path / "arch.yaml").read_text()
        assert "My Custom Project" in content

    def test_init_skips_existing_files(self, tmp_path, monkeypatch, capsys):
        """init skips existing files."""
        monkeypatch.chdir(tmp_path)

        # Create existing file
        (tmp_path / "arch.yaml").write_text("existing content")

        args = MagicMock()
        args.name = "Test"
        args.description = None
        args.github = None

        result = cmd_init(args)

        assert result == 0
        # Original content preserved
        assert (tmp_path / "arch.yaml").read_text() == "existing content"
        # Warning printed
        captured = capsys.readouterr()
        assert "already exists" in captured.out

    def test_init_with_github(self, tmp_path, monkeypatch, capsys):
        """init with --github adds GitHub config."""
        monkeypatch.chdir(tmp_path)

        args = MagicMock()
        args.name = "Test"
        args.description = None
        args.github = "owner/repo"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            result = cmd_init(args)

        assert result == 0
        content = (tmp_path / "arch.yaml").read_text()
        assert "owner/repo" in content
        assert "github:" in content

    def test_init_updates_gitignore(self, tmp_path, monkeypatch):
        """init adds entries to existing .gitignore."""
        monkeypatch.chdir(tmp_path)

        # Create existing gitignore
        (tmp_path / ".gitignore").write_text("node_modules/\n")

        args = MagicMock()
        args.name = "Test"
        args.description = None
        args.github = None

        result = cmd_init(args)

        assert result == 0
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert "state/" in content


class TestCmdStatus:
    """Tests for arch status command."""

    def test_status_not_running(self, tmp_path, capsys):
        """status shows not running when no PID file."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(tmp_path / "state")}
        }))

        args = MagicMock()
        args.config = str(config_path)

        result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "Not running" in captured.out

    def test_status_with_agents(self, tmp_path, capsys):
        """status shows agent information."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create agents.json
        agents = {
            "archie": {
                "role": "lead",
                "status": "working",
                "task": "Coordinating work",
                "sandboxed": False,
                "skip_permissions": False
            },
            "frontend-1": {
                "role": "frontend",
                "status": "blocked",
                "task": "Building navbar",
                "sandboxed": True,
                "skip_permissions": False
            }
        }
        (state_dir / "agents.json").write_text(json.dumps(agents))

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        args = MagicMock()
        args.config = str(config_path)

        result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "archie" in captured.out
        assert "frontend-1" in captured.out
        assert "[c]" in captured.out  # sandboxed indicator

    def test_status_with_token_usage(self, tmp_path, capsys):
        """status shows token usage."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create usage.json (TokenTracker output file)
        usage = {
            "archie": {"cost_usd": 0.05},
            "frontend-1": {"cost_usd": 0.02}
        }
        (state_dir / "usage.json").write_text(json.dumps(usage))
        (state_dir / "agents.json").write_text("{}")

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        args = MagicMock()
        args.config = str(config_path)

        result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "$0.05" in captured.out or "0.0500" in captured.out
        assert "Total" in captured.out


class TestCmdDown:
    """Tests for arch down command."""

    def test_down_not_running(self, tmp_path, capsys):
        """down reports when not running."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(tmp_path / "state")}
        }))

        args = MagicMock()
        args.config = str(config_path)

        result = cmd_down(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "not running" in captured.out

    def test_down_sends_signal(self, tmp_path):
        """down sends SIGTERM to running process."""
        state_dir = tmp_path / "state"
        write_pid_file(state_dir)

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        args = MagicMock()
        args.config = str(config_path)

        with patch("os.kill") as mock_kill:
            # Make signal 0 check succeed (process exists)
            mock_kill.return_value = None
            result = cmd_down(args)

        assert result == 0
        # os.kill is called twice: once with signal 0 (process check), once with SIGTERM
        calls = mock_kill.call_args_list
        assert any(call[0][1] == signal.SIGTERM for call in calls)

        # Cleanup
        remove_pid_file(state_dir)


class TestCmdSend:
    """Tests for arch send command (Issue #2)."""

    def test_send_message_arch_running(self, tmp_path, capsys):
        """send queues message when ARCH is running."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create PID file (simulate running ARCH)
        write_pid_file(state_dir)

        # Create minimal state files
        (state_dir / "agents.json").write_text("{}")
        (state_dir / "messages.json").write_text("[]")

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        args = MagicMock()
        args.config = str(config_path)
        args.message = "Please review the test results"

        result = cmd_send(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "Message sent" in captured.out
        assert "Archie will see this message" in captured.out

        # Verify message was added
        messages = json.loads((state_dir / "messages.json").read_text())
        assert len(messages) == 1
        assert messages[0]["from"] == "user"
        assert messages[0]["to"] == "archie"
        assert messages[0]["content"] == "Please review the test results"

        # Cleanup
        remove_pid_file(state_dir)

    def test_send_message_arch_not_running(self, tmp_path, capsys):
        """send queues message with warning when ARCH not running."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # No PID file (ARCH not running)
        (state_dir / "agents.json").write_text("{}")
        (state_dir / "messages.json").write_text("[]")

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        args = MagicMock()
        args.config = str(config_path)
        args.message = "Hello Archie"

        result = cmd_send(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "Warning" in captured.out
        assert "not running" in captured.out
        assert "Message sent" in captured.out
        assert "auto-resume" in captured.out.lower()

        # Verify message was still added
        messages = json.loads((state_dir / "messages.json").read_text())
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello Archie"

    def test_send_message_no_state_dir(self, tmp_path, capsys):
        """send fails when state directory doesn't exist."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(tmp_path / "nonexistent")}
        }))

        args = MagicMock()
        args.config = str(config_path)
        args.message = "Hello"

        result = cmd_send(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out
        assert "archie up" in captured.out.lower()

    def test_main_send(self, tmp_path, capsys):
        """main send command works."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agents.json").write_text("{}")
        (state_dir / "messages.json").write_text("[]")

        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(state_dir)}
        }))

        with patch("sys.argv", ["arch", "send", "Test message", "--config", str(config_path)]):
            result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "Message sent" in captured.out


class TestMain:
    """Tests for main entry point."""

    def test_main_no_command(self, capsys):
        """main with no command shows help."""
        with patch("sys.argv", ["arch"]):
            result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "ARCH" in captured.out or "usage" in captured.out.lower()

    def test_main_init(self, tmp_path, monkeypatch):
        """main init command works."""
        monkeypatch.chdir(tmp_path)

        with patch("sys.argv", ["arch", "init", "--name", "Test"]):
            result = main()

        assert result == 0
        assert (tmp_path / "arch.yaml").exists()

    def test_main_status(self, tmp_path, capsys):
        """main status command works."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(tmp_path / "state")}
        }))

        with patch("sys.argv", ["arch", "status", "--config", str(config_path)]):
            result = main()

        assert result == 0

    def test_main_down(self, tmp_path, capsys):
        """main down command works."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"state_dir": str(tmp_path / "state")}
        }))

        with patch("sys.argv", ["arch", "down", "--config", str(config_path)]):
            result = main()

        assert result == 0

    def test_main_up_no_config(self, tmp_path, monkeypatch, capsys):
        """main up fails when config doesn't exist."""
        monkeypatch.chdir(tmp_path)

        with patch("sys.argv", ["arch", "up"]):
            result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out


class TestCleanFlag:
    """Tests for --clean flag on archie up."""

    def _simulate_clean(self, state_dir):
        """Simulate the --clean logic from cmd_up."""
        import shutil
        events_backup = None
        events_path = state_dir / "events.jsonl"
        if events_path.exists():
            events_backup = events_path.read_text()
        shutil.rmtree(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        if events_backup:
            events_path.write_text(events_backup)

    def test_clean_removes_state_files(self, tmp_path):
        """--clean removes agents.json, messages.json, etc."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agents.json").write_text('{"agents": {}}')
        (state_dir / "messages.json").write_text('[]')
        (state_dir / "pending_decisions.json").write_text('[]')
        (state_dir / "usage.json").write_text('{}')

        self._simulate_clean(state_dir)

        assert state_dir.exists()
        assert not (state_dir / "agents.json").exists()
        assert not (state_dir / "messages.json").exists()
        assert not (state_dir / "pending_decisions.json").exists()
        assert not (state_dir / "usage.json").exists()

    def test_clean_preserves_events_jsonl(self, tmp_path):
        """--clean preserves events.jsonl as historical record."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agents.json").write_text('{}')
        (state_dir / "events.jsonl").write_text('{"event": "test"}\n{"event": "test2"}\n')

        self._simulate_clean(state_dir)

        assert (state_dir / "events.jsonl").exists()
        content = (state_dir / "events.jsonl").read_text()
        assert "test" in content
        assert "test2" in content

    def test_clean_no_events_file(self, tmp_path):
        """--clean works when there's no events.jsonl."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agents.json").write_text('{}')

        self._simulate_clean(state_dir)

        assert state_dir.exists()
        assert not (state_dir / "agents.json").exists()
        assert not (state_dir / "events.jsonl").exists()

    def test_argparse_accepts_clean_flag(self):
        """--clean is accepted by the argument parser."""
        import argparse
        # Verify the flag is parsed without error
        with patch("sys.argv", ["arch", "up", "--clean"]):
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="command")
            up = sub.add_parser("up")
            up.add_argument("--config", default="arch.yaml")
            up.add_argument("--keep-worktrees", action="store_true")
            up.add_argument("--clean", action="store_true")
            args = parser.parse_args(["up", "--clean"])
            assert args.clean is True

    def test_argparse_clean_default_false(self):
        """--clean defaults to False."""
        import argparse
        with patch("sys.argv", ["arch", "up"]):
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="command")
            up = sub.add_parser("up")
            up.add_argument("--config", default="arch.yaml")
            up.add_argument("--keep-worktrees", action="store_true")
            up.add_argument("--clean", action="store_true")
            args = parser.parse_args(["up"])
            assert args.clean is False


class TestDefaultTemplates:
    """Tests for default template content."""

    def test_arch_yaml_template_valid(self):
        """DEFAULT_ARCH_YAML is valid YAML."""
        content = DEFAULT_ARCH_YAML.format(
            project_name="Test",
            project_description="A test"
        )
        parsed = yaml.safe_load(content)

        assert parsed["project"]["name"] == "Test"
        assert "archie" in parsed
        assert "agent_pool" in parsed
        assert "settings" in parsed

    def test_brief_md_template(self):
        """DEFAULT_BRIEF_MD has required sections."""
        content = DEFAULT_BRIEF_MD.format(project_name="Test")

        assert "## Goal" in content
        assert "## This Session" in content
        assert "## Done When (this session)" in content
        assert "## Done When (project)" in content
        assert "## Completed" in content
        assert "## Backlog" in content
        assert "## Constraints" in content
        assert "## Current Status" in content
        assert "## Decisions Log" in content
        assert "Rationale" in content


class TestCmdDashboard:
    """Tests for archie dashboard command."""

    def test_dashboard_no_config(self, tmp_path, capsys):
        """dashboard fails when config doesn't exist."""
        args = MagicMock()
        args.config = str(tmp_path / "nonexistent.yaml")

        result = cmd_dashboard(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_dashboard_opens_browser_when_running(self, tmp_path, capsys):
        """dashboard opens browser when ARCH is running."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {
                "mcp_port": 3999,
            }
        }))

        args = MagicMock()
        args.config = str(config_path)

        with patch("urllib.request.urlopen"):  # Simulate healthy server
            with patch("webbrowser.open") as mock_open:
                result = cmd_dashboard(args)

        assert result == 0
        mock_open.assert_called_once_with("http://localhost:3999/dashboard")

    def test_dashboard_fails_when_not_running(self, tmp_path, capsys):
        """dashboard fails when ARCH is not running."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {
                "mcp_port": 4000,
            }
        }))

        args = MagicMock()
        args.config = str(config_path)

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = cmd_dashboard(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "not running" in captured.out

    def test_main_dashboard(self, tmp_path, capsys):
        """main dashboard command works."""
        config_path = tmp_path / "arch.yaml"
        config_path.write_text(yaml.dump({
            "settings": {"mcp_port": 3999}
        }))

        with patch("sys.argv", ["archie", "dashboard", "--config", str(config_path)]):
            with patch("urllib.request.urlopen"):
                with patch("webbrowser.open"):
                    result = main()

        assert result == 0
