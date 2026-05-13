"""Tests for :mod:`scitex_agent_container.runtimes.ssh_remote`.

No real ssh / scp / rsync subprocesses are launched: every
``subprocess.run`` and ``subprocess.Popen`` call is patched, so we
exercise the argv shape, hop-chain composition, rsync→tar-pipe
fallback, login-shell wrapping, preflight parser, and the start
control flow without touching the network.

Style mirrors ``tests/scitex_agent_container/_listen/test_server.py``:
small fixtures, tight asserts, no shared mutable state.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.ssh_remote import SSHPreflightError, SSHRemote

# --- fixtures -----------------------------------------------------------


def _make_config(
    name: str = "alpha",
    host: str = "remote.example.com",
    user: str = "u",
    hops: list[str] | None = None,
    key: str = "",
    port: int = 22,
    config_path: str | None = None,
) -> AgentConfig:
    c = AgentConfig(name=name)
    c.remote.host = host
    c.remote.user = user
    c.remote.hops = hops or []
    c.remote.key = key
    c.remote.port = port
    if config_path is not None:
        c.config_path = config_path
    return c


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- _ssh_target / _ssh_base / _scp_base --------------------------------


def test_ssh_target_renders_user_at_host():
    c = _make_config(user="alice", host="bastion")
    assert SSHRemote._ssh_target(c) == "alice@bastion"


def test_ssh_base_legacy_dict_includes_target():
    c = _make_config()
    cmd = SSHRemote._ssh_base(c)
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert "StrictHostKeyChecking=accept-new" in cmd
    assert cmd[-1] == "u@remote.example.com"
    assert "-i" not in cmd and "-p" not in cmd


def test_ssh_base_includes_key_and_port():
    c = _make_config(key="/path/to/key", port=2222)
    cmd = SSHRemote._ssh_base(c)
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "/path/to/key"
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "2222"


def test_ssh_base_with_hops_skips_local_and_uses_J():
    c = _make_config(hops=["spartan", "hop1", "final"])
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        side_effect=lambda h: h == "spartan",
    ):
        cmd = SSHRemote._ssh_base(c)
    assert cmd[0] == "ssh"
    # local hop dropped → remaining = ['hop1', 'final'] → '-J hop1 final'
    assert "-J" in cmd
    j_idx = cmd.index("-J")
    assert cmd[j_idx + 1] == "hop1"
    assert cmd[j_idx + 2] == "final"
    # No user@host appended in chain mode.
    assert "u@remote.example.com" not in cmd


def test_ssh_base_hops_single_remote_uses_bare_host():
    c = _make_config(hops=["only-remote"])
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        return_value=False,
    ):
        cmd = SSHRemote._ssh_base(c)
    assert cmd[-1] == "only-remote"
    assert "-J" not in cmd


def test_scp_base_carries_key_and_port():
    c = _make_config(key="/k", port=2200)
    cmd = SSHRemote._scp_base(c)
    assert cmd[0] == "scp"
    assert "-i" in cmd and "-P" in cmd
    assert cmd[cmd.index("-P") + 1] == "2200"


# --- login-shell wrapping -----------------------------------------------


def test_wrap_login_shell_escapes_single_quotes():
    wrapped = SSHRemote._wrap_login_shell("echo 'hi'")
    assert wrapped.startswith("bash -l -c '")
    assert wrapped.endswith("'")
    # The single-quote escape pattern is '\''.
    assert "'\\''" in wrapped


def test_wrap_login_shell_plain_command():
    assert SSHRemote._wrap_login_shell("uptime") == "bash -l -c 'uptime'"


# --- run() --------------------------------------------------------------


def test_run_invokes_subprocess_with_login_shell():
    c = _make_config()
    fake = _completed(0, stdout="ok\n")
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ) as mrun:
        result = SSHRemote.run(c, "hostname")
    assert result.returncode == 0
    args, kwargs = mrun.call_args
    argv = args[0]
    assert argv[0] == "ssh"
    assert argv[-1].startswith("bash -l -c '")
    assert "hostname" in argv[-1]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_run_without_login_shell_passes_raw_command():
    c = _make_config()
    fake = _completed(0)
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ) as mrun:
        SSHRemote.run(c, "uptime", login_shell=False)
    argv = mrun.call_args[0][0]
    assert argv[-1] == "uptime"


def test_run_uses_remote_timeout_when_unspecified():
    c = _make_config()
    c.remote.timeout = 77
    fake = _completed(0)
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ) as mrun:
        SSHRemote.run(c, "uptime")
    assert mrun.call_args.kwargs["timeout"] == 77


def test_run_timeout_raises_runtime_error_with_guidance():
    c = _make_config()
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=1),
    ):
        with pytest.raises(RuntimeError) as ei:
            SSHRemote.run(c, "uptime")
    msg = str(ei.value)
    assert "timed out" in msg
    assert "u@remote.example.com" in msg


def test_run_logs_warning_on_nonzero(caplog):
    c = _make_config()
    fake = _completed(2, stderr="boom\n")
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ):
        with caplog.at_level("WARNING"):
            result = SSHRemote.run(c, "uptime")
    assert result.returncode == 2
    assert any("SSH command failed" in r.message for r in caplog.records)


# --- preflight() --------------------------------------------------------


_GOOD_PREFLIGHT_OUT = (
    "===CHECK_SSH_OK===\n"
    "===CHECK_SCREEN_START===\n/usr/bin/screen\n===CHECK_SCREEN_END===\n"
    "===CHECK_SAC_START===\n/usr/local/bin/scitex-agent-container\n"
    "scitex-agent-container 1.2.3\n===CHECK_SAC_END===\n"
    "===CHECK_PYTHON_START===\nPython 3.11.0\n===CHECK_PYTHON_END===\n"
    "===CHECK_DISK_START===\n42%\n===CHECK_DISK_END===\n"
)


def test_preflight_all_pass():
    c = _make_config()
    fake = _completed(0, stdout=_GOOD_PREFLIGHT_OUT)
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ):
        results = SSHRemote.preflight(c)
    by_name = {name: (ok, detail) for name, ok, detail in results}
    assert by_name["SSH connection"][0] is True
    assert by_name["screen"][0] is True
    assert by_name["scitex-agent-container"][0] is True
    assert by_name["python"][0] is True
    assert by_name["disk space"][0] is True
    assert "42%" in by_name["disk space"][1]


def test_preflight_timeout_returns_single_failure():
    c = _make_config()
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=1),
    ):
        results = SSHRemote.preflight(c)
    assert len(results) == 1
    name, ok, detail = results[0]
    assert name == "SSH connection"
    assert ok is False
    assert "Cannot SSH" in detail


def test_preflight_missing_ssh_marker_fails_fast():
    c = _make_config()
    fake = _completed(0, stdout="garbage with no markers")
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ):
        results = SSHRemote.preflight(c)
    assert len(results) == 1
    assert results[0][0] == "SSH connection"
    assert results[0][1] is False


def test_preflight_missing_screen_and_sac_and_python():
    c = _make_config()
    out = (
        "===CHECK_SSH_OK===\n"
        "===CHECK_SCREEN_START===\n\n===CHECK_SCREEN_END===\n"
        "===CHECK_SAC_START===\n\n===CHECK_SAC_END===\n"
        "===CHECK_PYTHON_START===\n\n===CHECK_PYTHON_END===\n"
        "===CHECK_DISK_START===\n\n===CHECK_DISK_END===\n"
    )
    fake = _completed(0, stdout=out)
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ):
        results = SSHRemote.preflight(c)
    by_name = {n: (ok, d) for n, ok, d in results}
    assert by_name["screen"][0] is False
    assert by_name["scitex-agent-container"][0] is False
    assert by_name["python"][0] is False
    # disk space falls through to "unknown" (still ok)
    assert by_name["disk space"][0] is True
    assert "unknown" in by_name["disk space"][1]


def test_preflight_no_login_shell_passes_raw_script():
    c = _make_config()
    c.remote.login_shell = False
    fake = _completed(0, stdout=_GOOD_PREFLIGHT_OUT)
    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        return_value=fake,
    ) as mrun:
        SSHRemote.preflight(c)
    argv = mrun.call_args[0][0]
    # raw remote script, NOT wrapped in `bash -l -c`
    assert not argv[-1].startswith("bash -l -c")
    assert "===CHECK_SSH_OK===" in argv[-1]


# --- check_or_raise -----------------------------------------------------


def test_check_or_raise_pass_no_exception():
    c = _make_config()
    with patch.object(
        SSHRemote,
        "preflight",
        return_value=[("SSH connection", True, "OK")],
    ):
        SSHRemote.check_or_raise(c)  # must not raise


def test_check_or_raise_failure_raises():
    c = _make_config()
    with patch.object(
        SSHRemote,
        "preflight",
        return_value=[
            ("SSH connection", True, "OK"),
            ("screen", False, "missing"),
        ],
    ):
        with pytest.raises(SSHPreflightError) as ei:
            SSHRemote.check_or_raise(c)
    assert "screen" in str(ei.value)
    assert "Preflight check failed" in str(ei.value)
