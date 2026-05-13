"""Deploy / copy / lifecycle tests for ``SSHRemote``.

Split from ``test_ssh_remote.py`` to keep each test file under the
512-line cap. Covers:

* ``copy_config`` — rsync happy path, remote-section stripping, cat
  failure / timeout, rsync → tar-pipe fallback (both nonzero-rc and
  FileNotFoundError flavours).
* ``start`` — success path, ``--force`` passthrough, ``no_preflight``
  short-circuit, screen-diagnostic capture on failure, diag-exception
  safety net.
* ``stop`` / ``is_running`` / ``logs`` — happy and failure paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.ssh_remote import SSHRemote


def _make_config(
    name: str = "alpha",
    host: str = "remote.example.com",
    user: str = "u",
    config_path: str | None = None,
) -> AgentConfig:
    c = AgentConfig(name=name)
    c.remote.host = host
    c.remote.user = user
    if config_path is not None:
        c.config_path = config_path
    return c


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _seed_yaml(tmp_path: Path, with_remote: bool = True) -> Path:
    yaml_path = tmp_path / "spec.yaml"
    body = "apiVersion: x\nkind: Agent\nspec:\n  runtime: apptainer\n"
    if with_remote:
        body += "  remote:\n    host: r\n    user: u\n"
    yaml_path.write_text(body)
    return yaml_path


# --- copy_config --------------------------------------------------------


def test_copy_config_requires_config_path():
    c = _make_config()
    with pytest.raises(RuntimeError, match="config_path is not set"):
        SSHRemote.copy_config(c)


def test_copy_config_rsync_path(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    (tmp_path / "dot_claude").mkdir()
    (tmp_path / "dot_claude" / "CLAUDE.md").write_text("hi")
    c = _make_config(config_path=str(yaml_path))

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _completed(0)

    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=fake_run,
    ):
        remote_path = SSHRemote.copy_config(c)

    assert remote_path.endswith("/spec.yaml")
    assert len(calls) == 3  # mkdir, cat-write, rsync
    assert calls[0][0] == "ssh"
    assert any("mkdir -p" in tok for tok in calls[0])
    assert calls[1][0] == "ssh"
    assert any("cat >" in tok for tok in calls[1])
    assert calls[2][0] == "rsync"
    assert "--delete" in calls[2]


def test_copy_config_strips_remote_section(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    c = _make_config(config_path=str(yaml_path))

    cat_inputs: list[str] = []

    def fake_run(cmd, **kw):
        if "input" in kw and kw["input"] is not None:
            cat_inputs.append(kw["input"])
        return _completed(0)

    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=fake_run,
    ):
        SSHRemote.copy_config(c)

    assert cat_inputs
    assert "remote:" not in cat_inputs[0]


def test_copy_config_cat_failure_raises(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    c = _make_config(config_path=str(yaml_path))

    n = {"i": 0}

    def fake_run(cmd, **kw):
        n["i"] += 1
        if n["i"] == 2:
            return _completed(1, stderr="permission denied")
        return _completed(0)

    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=fake_run,
    ):
        with pytest.raises(RuntimeError, match="Failed to copy config"):
            SSHRemote.copy_config(c)


def test_copy_config_cat_timeout_raises(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    c = _make_config(config_path=str(yaml_path))

    with patch(
        "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=1),
    ):
        with pytest.raises(RuntimeError, match="Timed out copying config"):
            SSHRemote.copy_config(c)


def _tar_proc():
    p = MagicMock()
    p.stdout = MagicMock()
    p.__enter__ = lambda self: p
    p.__exit__ = lambda self, *a: False
    return p


def test_copy_config_rsync_fallback_to_tar_pipe(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    (tmp_path / "dot_claude").mkdir()
    (tmp_path / "dot_claude" / "x.md").write_text("x")
    c = _make_config(config_path=str(yaml_path))

    run_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        run_calls.append(list(cmd))
        if cmd and cmd[0] == "rsync":
            return _completed(23, stderr="rsync: command not found")
        return _completed(0)

    with (
        patch(
            "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
            side_effect=fake_run,
        ),
        patch(
            "scitex_agent_container.runtimes.ssh_remote.subprocess.Popen",
            return_value=_tar_proc(),
        ) as mpopen,
    ):
        SSHRemote.copy_config(c)

    popen_args = mpopen.call_args[0][0]
    assert popen_args[0] == "tar"
    assert "-cz" in popen_args
    assert "dot_claude" in popen_args
    assert any("tar -xz" in tok for cmd in run_calls for tok in cmd)


def test_copy_config_rsync_filenotfound_falls_back(tmp_path):
    yaml_path = _seed_yaml(tmp_path, with_remote=True)
    (tmp_path / "dot_claude").mkdir()
    c = _make_config(config_path=str(yaml_path))

    def fake_run(cmd, **kw):
        if cmd and cmd[0] == "rsync":
            raise FileNotFoundError("rsync")
        return _completed(0)

    with (
        patch(
            "scitex_agent_container.runtimes.ssh_remote.subprocess.run",
            side_effect=fake_run,
        ),
        patch(
            "scitex_agent_container.runtimes.ssh_remote.subprocess.Popen",
            return_value=_tar_proc(),
        ) as mpopen,
    ):
        SSHRemote.copy_config(c)

    assert mpopen.called


# --- start / stop / is_running / logs -----------------------------------


def test_start_success_skips_diagnostics():
    c = _make_config(config_path="/tmp/spec.yaml")
    with (
        patch.object(SSHRemote, "check_or_raise"),
        patch.object(SSHRemote, "copy_config", return_value="/remote/spec.yaml"),
        patch.object(SSHRemote, "run", return_value=_completed(0)) as mrun,
    ):
        assert SSHRemote.start(c) is True
    assert mrun.call_count == 1
    sent_cmd = mrun.call_args[0][1]
    assert "scitex-agent-container start /remote/spec.yaml" in sent_cmd
    assert "--force" not in sent_cmd


def test_start_force_passes_force_flag():
    c = _make_config(config_path="/tmp/spec.yaml")
    with (
        patch.object(SSHRemote, "check_or_raise"),
        patch.object(SSHRemote, "copy_config", return_value="/r/spec.yaml"),
        patch.object(SSHRemote, "run", return_value=_completed(0)) as mrun,
    ):
        SSHRemote.start(c, force=True)
    assert " --force " in mrun.call_args[0][1]


def test_start_no_preflight_skips_check():
    c = _make_config(config_path="/tmp/spec.yaml")
    with (
        patch.object(SSHRemote, "check_or_raise") as mchk,
        patch.object(SSHRemote, "copy_config", return_value="/r/spec.yaml"),
        patch.object(SSHRemote, "run", return_value=_completed(0)),
    ):
        SSHRemote.start(c, no_preflight=True)
    mchk.assert_not_called()


def test_start_failure_captures_screen_diag_and_raises():
    c = _make_config(config_path="/tmp/spec.yaml")
    fail = _completed(1, stderr="boom")
    diag = _completed(0, stdout="screen diagnostic output")
    with (
        patch.object(SSHRemote, "check_or_raise"),
        patch.object(SSHRemote, "copy_config", return_value="/r/spec.yaml"),
        patch.object(SSHRemote, "run", side_effect=[fail, diag]),
    ):
        with pytest.raises(RuntimeError) as ei:
            SSHRemote.start(c)
    msg = str(ei.value)
    assert "Failed to start agent" in msg
    assert "screen diagnostic output" in msg


def test_start_failure_diag_exception_safe():
    c = _make_config(config_path="/tmp/spec.yaml")
    fail = _completed(1, stderr="boom")
    with (
        patch.object(SSHRemote, "check_or_raise"),
        patch.object(SSHRemote, "copy_config", return_value="/r/spec.yaml"),
        patch.object(
            SSHRemote, "run", side_effect=[fail, RuntimeError("diag exploded")]
        ),
    ):
        with pytest.raises(RuntimeError) as ei:
            SSHRemote.start(c)
    assert "could not capture screen output" in str(ei.value)


def test_stop_success():
    c = _make_config()
    with patch.object(SSHRemote, "run", return_value=_completed(0)) as mrun:
        assert SSHRemote.stop(c) is True
    assert "stop alpha" in mrun.call_args[0][1]


def test_stop_failure_returns_false(caplog):
    c = _make_config()
    with patch.object(
        SSHRemote, "run", return_value=_completed(1, stderr="no such session")
    ):
        with caplog.at_level("ERROR"):
            assert SSHRemote.stop(c) is False
    assert any("Failed to stop" in r.message for r in caplog.records)


def test_is_running_true_when_screen_name_in_stdout():
    c = _make_config()
    out = _completed(0, stdout="There is a screen on:\n\t1234.cld-alpha")
    with patch.object(SSHRemote, "run", return_value=out):
        assert SSHRemote.is_running(c) is True


def test_is_running_false_on_exception():
    c = _make_config()
    with patch.object(SSHRemote, "run", side_effect=RuntimeError("net down")):
        assert SSHRemote.is_running(c) is False


def test_is_running_false_when_name_absent():
    c = _make_config()
    out = _completed(0, stdout="No Sockets found")
    with patch.object(SSHRemote, "run", return_value=out):
        assert SSHRemote.is_running(c) is False


def test_logs_success_returns_stdout():
    c = _make_config()
    with patch.object(
        SSHRemote, "run", return_value=_completed(0, stdout="log lines\n")
    ):
        assert SSHRemote.logs(c, lines=10) == "log lines\n"


def test_logs_failure_returns_error_string():
    c = _make_config()
    with patch.object(
        SSHRemote, "run", return_value=_completed(2, stderr="permission")
    ):
        out = SSHRemote.logs(c)
    assert out.startswith("[SSH error]")
    assert "permission" in out
