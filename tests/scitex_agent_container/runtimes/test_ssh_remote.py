"""Tests for :mod:`scitex_agent_container.runtimes.ssh_remote`.

Two layers of coverage:

1. Pure argv-shape / string-rendering tests (``_ssh_target``, ``_ssh_base``,
   ``_scp_base``, ``_wrap_login_shell``).

2. Lifecycle integration tests that exercise ``preflight``, ``run``,
   ``copy_config``, ``start``, ``stop``, ``is_running``, ``logs`` via real
   ``subprocess.run`` calls against PATH-installed fake binaries (``ssh``,
   ``scp``, ``rsync``, ``tar``) provided by the ``subprocess_shim``
   fixture. No ``patch``/``monkeypatch`` of ``subprocess.run`` — the real
   PATH lookup finds the shim, executes it, and the shim records its
   argv to a log file the assertions read back.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.ssh_remote import (
    SSHPreflightError,
    SSHRemote,
)


def _make_config(
    name: str = "alpha",
    host: str = "remote.example.com",
    user: str = "u",
    hops: list[str] | None = None,
    key: str = "",
    port: int = 22,
) -> AgentConfig:
    c = AgentConfig(name=name)
    c.remote.host = host
    c.remote.user = user
    c.remote.hops = hops or []
    c.remote.key = key
    c.remote.port = port
    return c


# ---------------------------------------------------------------------------
# _ssh_target / _ssh_base / _scp_base — pure argv-shape tests.
# ---------------------------------------------------------------------------


def test_ssh_target_renders_user_at_host():
    # Arrange
    c = _make_config(user="alice", host="bastion")
    # Act
    target = SSHRemote._ssh_target(c)
    # Assert
    assert target == "alice@bastion"


def test_ssh_base_uses_ssh_as_argv0():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[0] == "ssh"


def test_ssh_base_sets_batch_mode_yes():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "BatchMode=yes" in cmd


def test_ssh_base_sets_strict_host_key_accept_new():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "StrictHostKeyChecking=accept-new" in cmd


def test_ssh_base_target_is_last_arg_when_no_hops():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[-1] == "u@remote.example.com"


def test_ssh_base_omits_key_flag_when_key_empty():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "-i" not in cmd


def test_ssh_base_omits_port_flag_when_default():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "-p" not in cmd


def test_ssh_base_includes_key_when_set():
    # Arrange
    c = _make_config(key="/path/to/key")
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[cmd.index("-i") + 1] == "/path/to/key"


def test_ssh_base_includes_port_when_set():
    # Arrange
    c = _make_config(port=2222)
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[cmd.index("-p") + 1] == "2222"


def test_scp_base_uses_scp_as_argv0():
    # Arrange
    c = _make_config(key="/k", port=2200)
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert cmd[0] == "scp"


def test_scp_base_uses_capital_p_for_port():
    # Arrange — scp uses -P (capital) for port, unlike ssh's -p.
    c = _make_config(port=2200)
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert cmd[cmd.index("-P") + 1] == "2200"


def test_scp_base_includes_key_when_set():
    # Arrange
    c = _make_config(key="/k")
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert "-i" in cmd


# ---------------------------------------------------------------------------
# _wrap_login_shell — pure string transformation.
# ---------------------------------------------------------------------------


def test_wrap_login_shell_starts_with_bash_dash_l():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped.startswith("bash -l -c '")


def test_wrap_login_shell_ends_with_closing_quote():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped.endswith("'")


def test_wrap_login_shell_plain_command_round_trips():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped == "bash -l -c 'uptime'"


def test_wrap_login_shell_escapes_single_quote_via_backslash_pattern():
    # Arrange — bash's standard single-quote escape: '\''
    cmd = "echo 'hi'"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert "'\\''" in wrapped


# ---------------------------------------------------------------------------
# Lifecycle integration tests — real subprocess, PATH-shimmed ssh/scp/rsync.
# These exercise preflight / run / copy_config / start / stop / is_running /
# logs end-to-end via subprocess.run, with the shim providing controlled
# stdout / stderr / exit code so error-path branches are reachable too.
# ---------------------------------------------------------------------------


_PREFLIGHT_OK_STDOUT = (
    "===CHECK_SSH_OK===\n"
    "===CHECK_SCREEN_START===\n"
    "/usr/bin/screen\n"
    "===CHECK_SCREEN_END===\n"
    "===CHECK_SAC_START===\n"
    "/usr/local/bin/scitex-agent-container\n"
    "scitex-agent-container 0.15.0\n"
    "===CHECK_SAC_END===\n"
    "===CHECK_PYTHON_START===\n"
    "Python 3.11.6\n"
    "===CHECK_PYTHON_END===\n"
    "===CHECK_DISK_START===\n"
    "42%\n"
    "===CHECK_DISK_END===\n"
)


def test_preflight_invokes_real_ssh_binary(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_PREFLIGHT_OK_STDOUT, exit=0)
    c = _make_config()
    # Act
    SSHRemote.preflight(c)
    # Assert
    assert subprocess_shim.call_count("ssh") == 1


def test_preflight_passes_when_all_checks_ok(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_PREFLIGHT_OK_STDOUT, exit=0)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert all(passed for _, passed, _ in results)


def test_preflight_reports_ssh_failure_on_nonzero_exit(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="connection refused", exit=255)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert results[0] == ("SSH connection", False, results[0][2])


def test_preflight_returns_only_ssh_failure_when_marker_missing(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert len(results) == 1


def test_preflight_handles_missing_ssh_binary(env_save_restore, tmp_path):
    # Arrange — empty PATH guarantees FileNotFoundError from subprocess.run.
    env_save_restore.set("PATH", str(tmp_path / "empty"))
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert results[0][1] is False


def test_preflight_flags_missing_screen(subprocess_shim):
    # Arrange — screen which-output empty (no '/' in body).
    stdout = _PREFLIGHT_OK_STDOUT.replace("/usr/bin/screen\n", "\n")
    subprocess_shim.install("ssh", stdout=stdout, exit=0)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert ("screen", False) == (results[1][0], results[1][1])


def test_preflight_flags_missing_sac_binary(subprocess_shim):
    # Arrange — empty SAC body.
    stdout = _PREFLIGHT_OK_STDOUT.replace(
        "/usr/local/bin/scitex-agent-container\nscitex-agent-container 0.15.0\n",
        "\n",
    )
    subprocess_shim.install("ssh", stdout=stdout, exit=0)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert results[2][1] is False


def test_preflight_flags_missing_python(subprocess_shim):
    # Arrange
    stdout = _PREFLIGHT_OK_STDOUT.replace("Python 3.11.6\n", "\n")
    subprocess_shim.install("ssh", stdout=stdout, exit=0)
    c = _make_config()
    # Act
    results = SSHRemote.preflight(c)
    # Assert
    assert results[3][1] is False


def test_check_or_raise_raises_on_failure(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=255)
    c = _make_config()
    raised: Exception | None = None
    # Act
    try:
        SSHRemote.check_or_raise(c)
    except SSHPreflightError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, SSHPreflightError)


def test_check_or_raise_silent_on_success(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_PREFLIGHT_OK_STDOUT, exit=0)
    c = _make_config()
    # Act
    result = SSHRemote.check_or_raise(c)
    # Assert
    assert result is None


def test_run_returns_completed_process_on_success(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="hello\n", exit=0)
    c = _make_config()
    # Act
    result = SSHRemote.run(c, "echo hello")
    # Assert
    assert result.returncode == 0


def test_run_propagates_stdout(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="payload-xyz\n", exit=0)
    c = _make_config()
    # Act
    result = SSHRemote.run(c, "cat file")
    # Assert
    assert "payload-xyz" in result.stdout


def test_run_returns_nonzero_returncode_on_failure(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stderr="boom", exit=7)
    c = _make_config()
    # Act
    result = SSHRemote.run(c, "false")
    # Assert
    assert result.returncode == 7


def test_run_passes_remote_command_into_login_shell(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    # Act
    SSHRemote.run(c, "uptime", login_shell=True)
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert any("bash -l -c" in a for a in argv)


def test_run_without_login_shell_passes_command_raw(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    # Act
    SSHRemote.run(c, "uptime", login_shell=False)
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert argv[-1] == "uptime"


def test_copy_config_writes_yaml_via_ssh_stdin(subprocess_shim, tmp_path):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n  remote:\n    host: r\n")
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    remote_path = SSHRemote.copy_config(c)
    # Assert
    assert remote_path.endswith("/spec.yaml")


def test_copy_config_invokes_ssh_at_least_twice_for_mkdir_then_cat(
    subprocess_shim, tmp_path
):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    SSHRemote.copy_config(c)
    # Assert
    assert subprocess_shim.call_count("ssh") >= 2


def test_copy_config_raises_when_config_path_unset(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    c.config_path = ""
    raised: Exception | None = None
    # Act
    try:
        SSHRemote.copy_config(c)
    except RuntimeError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, RuntimeError)


def test_copy_config_raises_on_ssh_nonzero_exit(subprocess_shim, tmp_path):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    subprocess_shim.install("ssh", stdout="", stderr="denied", exit=1)
    c = _make_config()
    c.config_path = str(cfg)
    raised: Exception | None = None
    # Act
    try:
        SSHRemote.copy_config(c)
    except RuntimeError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, RuntimeError)


def test_copy_config_strips_remote_section_from_uploaded_yaml(
    subprocess_shim, tmp_path
):
    # Arrange — capture what ssh receives via a wrapper shim that records stdin.
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n  remote:\n    host: should-be-stripped\n")
    # Custom shim that records stdin too — install_with_stdin_capture pattern.
    import json
    import sys

    bin_dir = tmp_path / "_shim_bin2"
    bin_dir.mkdir()
    log = bin_dir / "ssh.stdin.log"
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({json.dumps(str(log))}, 'a').write(sys.stdin.read())\n"
    )
    script.chmod(0o755)
    import os

    saved = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved}"
    try:
        c = _make_config()
        c.config_path = str(cfg)
        # Act
        SSHRemote.copy_config(c)
        captured = log.read_text() if log.exists() else ""
    finally:
        os.environ["PATH"] = saved
    # Assert
    assert "should-be-stripped" not in captured


def test_copy_config_rsyncs_dot_claude_when_present(subprocess_shim, tmp_path):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    dc = tmp_path / "dot_claude"
    dc.mkdir()
    (dc / "CLAUDE.md").write_text("# hi\n")
    subprocess_shim.install("ssh", stdout="", exit=0)
    subprocess_shim.install("rsync", stdout="", exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    SSHRemote.copy_config(c)
    # Assert
    assert subprocess_shim.call_count("rsync") == 1


def test_copy_config_falls_back_to_tar_when_rsync_fails(subprocess_shim, tmp_path):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    dc = tmp_path / "dot_claude"
    dc.mkdir()
    (dc / "CLAUDE.md").write_text("# hi\n")
    subprocess_shim.install("ssh", stdout="", exit=0)
    subprocess_shim.install("rsync", stdout="", stderr="oops", exit=1)
    subprocess_shim.install("tar", stdout="", exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    SSHRemote.copy_config(c)
    # Assert
    assert subprocess_shim.call_count("tar") >= 1


def test_start_returns_true_on_success(subprocess_shim, tmp_path):
    # Arrange — ssh shim returns OK for both preflight and start calls.
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    subprocess_shim.install("ssh", stdout=_PREFLIGHT_OK_STDOUT, exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    ok = SSHRemote.start(c, no_preflight=True)
    # Assert
    assert ok is True


def test_start_raises_when_remote_command_fails(subprocess_shim, tmp_path):
    # Arrange — ssh shim returns nonzero so the post-copy start call fails.
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    subprocess_shim.install("ssh", stdout="", stderr="cannot start", exit=2)
    c = _make_config()
    c.config_path = str(cfg)
    raised: Exception | None = None
    # Act
    try:
        SSHRemote.start(c, no_preflight=True)
    except RuntimeError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, RuntimeError)


def test_start_with_force_passes_force_flag_to_remote(subprocess_shim, tmp_path):
    # Arrange
    cfg = tmp_path / "spec.yaml"
    cfg.write_text("spec:\n  name: alpha\n")
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    c.config_path = str(cfg)
    # Act
    SSHRemote.start(c, no_preflight=True, force=True)
    # Last invocation is the `scitex-agent-container start …` ssh call.
    last_argv = subprocess_shim.invocations("ssh")[-1]
    # Assert
    assert any("--force" in a for a in last_argv)


def test_stop_returns_true_on_zero_exit(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", exit=0)
    c = _make_config()
    # Act
    ok = SSHRemote.stop(c)
    # Assert
    assert ok is True


def test_stop_returns_false_on_nonzero_exit(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="no such agent", exit=1)
    c = _make_config()
    # Act
    ok = SSHRemote.stop(c)
    # Assert
    assert ok is False


def test_is_running_true_when_screen_name_in_stdout(subprocess_shim):
    # Arrange
    c = _make_config(name="alpha")
    subprocess_shim.install("ssh", stdout="\tcld-alpha\t(Detached)\n", exit=0)
    # Act
    running = SSHRemote.is_running(c)
    # Assert
    assert running is True


def test_is_running_false_when_screen_name_absent(subprocess_shim):
    # Arrange
    c = _make_config(name="alpha")
    subprocess_shim.install("ssh", stdout="No Sockets found.\n", exit=0)
    # Act
    running = SSHRemote.is_running(c)
    # Assert
    assert running is False


def test_logs_returns_stdout_on_success(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="line1\nline2\n", exit=0)
    c = _make_config()
    # Act
    out = SSHRemote.logs(c, lines=10)
    # Assert
    assert "line1" in out


def test_logs_returns_error_marker_on_failure(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="no logs", exit=1)
    c = _make_config()
    # Act
    out = SSHRemote.logs(c)
    # Assert
    assert out.startswith("[SSH error]")
