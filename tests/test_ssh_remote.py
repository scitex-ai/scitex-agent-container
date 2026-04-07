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


# -- Batched preflight output used by multiple tests --
_BATCHED_ALL_OK = (
    "===CHECK_SSH_OK===\n"
    "===CHECK_SCREEN_START===\n"
    "/usr/bin/screen\n"
    "===CHECK_SCREEN_END===\n"
    "===CHECK_SAC_START===\n"
    "/usr/local/bin/scitex-agent-container\n"
    "scitex-agent-container, version 0.3.0\n"
    "===CHECK_SAC_END===\n"
    "===CHECK_PYTHON_START===\n"
    "Python 3.11.5\n"
    "===CHECK_PYTHON_END===\n"
    "===CHECK_DISK_START===\n"
    "45%\n"
    "===CHECK_DISK_END===\n"
)


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
    def test_ssh_timeout_returns_early(self, mock_run):
        """When SSH times out, preflight should return immediately."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=60)
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        assert len(results) == 1
        name, passed, detail = results[0]
        assert name == "SSH connection"
        assert passed is False

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
        """When batched SSH command succeeds, all checks should pass."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_BATCHED_ALL_OK, stderr="",
        )
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        # SSH, screen, scitex-agent-container, python, disk space = 5 checks
        assert len(results) == 5
        for name, passed, detail in results:
            assert passed is True, f"{name} failed: {detail}"
        # Only 1 SSH call (batched)
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_screen_missing(self, mock_run):
        """When screen is missing from batched output, check should fail."""
        output = _BATCHED_ALL_OK.replace("/usr/bin/screen", "")
        mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        screen_result = [r for r in results if r[0] == "screen"][0]
        assert screen_result[1] is False

    @patch("subprocess.run")
    def test_sac_missing(self, mock_run):
        """When scitex-agent-container is missing, check should fail."""
        output = (
            "===CHECK_SSH_OK===\n"
            "===CHECK_SCREEN_START===\n/usr/bin/screen\n===CHECK_SCREEN_END===\n"
            "===CHECK_SAC_START===\n===CHECK_SAC_END===\n"
            "===CHECK_PYTHON_START===\nPython 3.11.5\n===CHECK_PYTHON_END===\n"
            "===CHECK_DISK_START===\n45%\n===CHECK_DISK_END===\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=output, stderr="")
        config = _make_remote_config()
        results = _SSHRemote.preflight(config)
        sac_result = [r for r in results if r[0] == "scitex-agent-container"][0]
        assert sac_result[1] is False


class TestPreflightLoginShell:
    @patch("subprocess.run")
    def test_login_shell_enabled(self, mock_run):
        """Preflight uses bash -l -c when login_shell is True (default)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_BATCHED_ALL_OK, stderr="",
        )
        config = _make_remote_config()
        _SSHRemote.preflight(config)
        cmd = mock_run.call_args[0][0]
        assert any("bash -l -c" in arg for arg in cmd)

    @patch("subprocess.run")
    def test_login_shell_disabled(self, mock_run):
        """Preflight skips bash -l -c when login_shell is False."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_BATCHED_ALL_OK, stderr="",
        )
        config = AgentConfig(
            name="test-agent",
            remote=RemoteSpec(host="testhost", user="testuser", login_shell=False),
        )
        _SSHRemote.preflight(config)
        cmd = mock_run.call_args[0][0]
        assert not any("bash -l -c" in arg for arg in cmd)


class TestNoPreflightFlag:
    @patch("subprocess.run")
    def test_start_skips_preflight(self, mock_run):
        """_SSHRemote.start() should skip preflight when no_preflight=True."""
        # Mock copy_config and the start command
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = _make_remote_config()
        config.config_path = "/tmp/test.yaml"

        with patch("builtins.open", create=True):
            with patch.object(_SSHRemote, "copy_config", return_value="/tmp/test.yaml"):
                with patch.object(_SSHRemote, "run", return_value=MagicMock(returncode=0)):
                    with patch.object(_SSHRemote, "check_or_raise") as mock_check:
                        result = _SSHRemote.start(config, no_preflight=True)
                        mock_check.assert_not_called()
                        assert result is True

    @patch("subprocess.run")
    def test_start_runs_preflight_by_default(self, mock_run):
        """_SSHRemote.start() should run preflight by default."""
        mock_run.return_value = MagicMock(returncode=0, stdout=_BATCHED_ALL_OK, stderr="")
        config = _make_remote_config()
        config.config_path = "/tmp/test.yaml"

        with patch.object(_SSHRemote, "copy_config", return_value="/tmp/test.yaml"):
            with patch.object(_SSHRemote, "run", return_value=MagicMock(returncode=0)):
                with patch.object(_SSHRemote, "check_or_raise") as mock_check:
                    _SSHRemote.start(config, no_preflight=False)
                    mock_check.assert_called_once()


class TestRemoteSpecLoginShell:
    def test_default_login_shell(self):
        """login_shell defaults to True."""
        r = RemoteSpec(host="h", user="u")
        assert r.login_shell is True

    def test_login_shell_false(self):
        """login_shell can be set to False."""
        r = RemoteSpec(host="h", user="u", login_shell=False)
        assert r.login_shell is False

    @patch("subprocess.run")
    def test_run_respects_config_login_shell(self, mock_run):
        """_SSHRemote.run() should default to config.remote.login_shell."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = AgentConfig(
            name="test",
            remote=RemoteSpec(host="h", user="u", login_shell=False),
        )
        _SSHRemote.run(config, "echo hello")
        cmd = mock_run.call_args[0][0]
        # Should NOT wrap in bash -l -c
        assert not any("bash -l -c" in arg for arg in cmd)
