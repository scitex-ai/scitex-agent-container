"""Tests for _SSHRemote helper in claude_code runtime."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from scitex_agent_container.runtimes.claude_code import _SSHRemote, SSHPreflightError
from scitex_agent_container.config import AgentConfig, RemoteSpec


def _make_remote_config(host: str = "testhost", user: str = "testuser") -> AgentConfig:
    """Create a minimal AgentConfig with remote settings."""
    return AgentConfig(
        name="test-agent",
        remote=RemoteSpec(host=host, user=user),
    )


class TestWrapLoginShell:
    def test_simple_command(self):
        result = _SSHRemote._wrap_login_shell("which screen")
        assert result == "bash -l -c 'which screen'"

    def test_command_with_single_quotes(self):
        result = _SSHRemote._wrap_login_shell("echo 'hello'")
        assert result == "bash -l -c 'echo '\\''hello'\\'''"

    def test_empty_command(self):
        result = _SSHRemote._wrap_login_shell("")
        assert result == "bash -l -c ''"


class TestSSHTarget:
    def test_user_at_host(self):
        config = _make_remote_config(host="myhost", user="myuser")
        assert _SSHRemote._ssh_target(config) == "myuser@myhost"


class TestSSHBase:
    def test_default_port(self):
        config = _make_remote_config()
        cmd = _SSHRemote._ssh_base(config)
        assert "testuser@testhost" in cmd
        assert "-p" not in cmd

    def test_custom_port(self):
        config = AgentConfig(
            name="test", remote=RemoteSpec(host="h", user="u", port=2222),
        )
        cmd = _SSHRemote._ssh_base(config)
        idx = cmd.index("-p")
        assert cmd[idx + 1] == "2222"

    def test_custom_key(self):
        config = AgentConfig(
            name="test", remote=RemoteSpec(host="h", user="u", key="/tmp/id_rsa"),
        )
        cmd = _SSHRemote._ssh_base(config)
        idx = cmd.index("-i")
        assert cmd[idx + 1] == "/tmp/id_rsa"


class TestPreflightSSHFailure:
    @patch("subprocess.run")
    def test_ssh_failure_returns_early(self, mock_run):
        """When SSH connection fails, preflight should return immediately."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Connection refused")
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        assert len(results) == 1
        name, passed, detail = results[0]
        assert name == "SSH connection"
        assert passed is False
        assert "ssh-copy-id" in detail

    @patch("subprocess.run")
    def test_check_or_raise_on_failure(self, mock_run):
        """check_or_raise should raise SSHPreflightError on failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="refused")
        config = _make_remote_config()
        try:
            _SSHRemote.check_or_raise(config)
            assert False, "Should have raised SSHPreflightError"
        except SSHPreflightError as exc:
            assert "ssh-copy-id" in str(exc)


class TestPreflightAllOK:
    @patch("subprocess.run")
    def test_all_checks_pass(self, mock_run):
        """When all SSH commands succeed, all checks should pass."""
        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            m = MagicMock(returncode=0)
            if "echo ok" in cmd_str:
                m.stdout = "ok\n"
            elif "which screen" in cmd_str:
                m.stdout = "/usr/bin/screen\n"
            elif "which scitex-agent-container" in cmd_str:
                m.stdout = "/usr/local/bin/scitex-agent-container\n"
            elif "--version" in cmd_str:
                m.stdout = "scitex-agent-container, version 0.2.0\n"
            elif "python3" in cmd_str:
                m.stdout = "Python 3.11.5\n"
            elif "df -h" in cmd_str:
                m.stdout = "Filesystem  Size  Used Avail Use% Mounted on\n/dev/sda1  100G  45G  55G  45% /\n"
            else:
                m.stdout = ""
            m.stderr = ""
            return m

        mock_run.side_effect = side_effect
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        # SSH, screen, scitex-agent-container, python, disk space = 5 checks
        assert len(results) == 5
        for name, passed, detail in results:
            assert passed is True, f"{name} failed: {detail}"
