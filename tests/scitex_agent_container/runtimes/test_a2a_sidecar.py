"""Tests for the A2A sidecar lifecycle (start/stop, no real subprocess).

The module under test is NOT a Starlette app — it's a thin
``subprocess.Popen`` launcher that runs ``python -m
scitex_agent_container a2a serve`` in the background. We mock
``Popen``, ``os.kill``, and the PID file FS interactions to exercise:

* spec.a2a parsing (missing / port-absent / valid)
* argv composition (host, port, handler, ``-v``, config path)
* stale PID file cleanup
* already-running detection (PID alive → no relaunch)
* spawn failure path
* stop with live / dead / corrupt PID files
"""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes import a2a_sidecar


def _make_config(tmp_path: Path, name: str = "alpha") -> AgentConfig:
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    yaml_path = tmp_path / "spec.yaml"
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  workdir: {workdir}\n"
        "  a2a:\n"
        "    port: 8123\n"
        "    host: 127.0.0.1\n"
        "    handler: echo\n"
    )
    c = AgentConfig(name=name, workdir=str(workdir))
    c.config_path = str(yaml_path)
    return c


# --- _read_a2a_block ----------------------------------------------------


def test_read_a2a_returns_block_when_port_set(tmp_path):
    c = _make_config(tmp_path)
    a2a = a2a_sidecar._read_a2a_block(c)
    assert a2a is not None
    assert a2a["port"] == 8123
    assert a2a["host"] == "127.0.0.1"


def test_read_a2a_returns_none_when_no_config_path(tmp_path):
    c = _make_config(tmp_path)
    c.config_path = ""
    assert a2a_sidecar._read_a2a_block(c) is None


def test_read_a2a_returns_none_when_yaml_missing(tmp_path):
    c = _make_config(tmp_path)
    Path(c.config_path).unlink()
    assert a2a_sidecar._read_a2a_block(c) is None


def test_read_a2a_returns_none_when_block_absent(tmp_path):
    c = _make_config(tmp_path)
    Path(c.config_path).write_text("spec:\n  runtime: apptainer\n")
    assert a2a_sidecar._read_a2a_block(c) is None


def test_read_a2a_returns_none_when_port_missing(tmp_path):
    c = _make_config(tmp_path)
    Path(c.config_path).write_text(
        "spec:\n  runtime: apptainer\n  a2a:\n    handler: echo\n"
    )
    assert a2a_sidecar._read_a2a_block(c) is None


def test_read_a2a_returns_none_on_malformed_yaml(tmp_path, caplog):
    c = _make_config(tmp_path)
    Path(c.config_path).write_text(":\n :\n -bad-yaml: [\n")
    with caplog.at_level("WARNING"):
        assert a2a_sidecar._read_a2a_block(c) is None


# --- _process_alive -----------------------------------------------------


def test_process_alive_true_when_kill0_ok():
    with patch("scitex_agent_container.runtimes.a2a_sidecar.os.kill") as mk:
        mk.return_value = None
        assert a2a_sidecar._process_alive(1234) is True


def test_process_alive_false_on_lookup_error():
    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.os.kill",
        side_effect=ProcessLookupError(),
    ):
        assert a2a_sidecar._process_alive(9999) is False


def test_process_alive_false_on_permission_error():
    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.os.kill",
        side_effect=PermissionError(),
    ):
        assert a2a_sidecar._process_alive(1) is False


def test_process_alive_false_on_os_error():
    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.os.kill",
        side_effect=OSError(),
    ):
        assert a2a_sidecar._process_alive(1) is False


# --- start_sidecar ------------------------------------------------------


def test_start_sidecar_returns_none_when_disabled(tmp_path):
    c = _make_config(tmp_path)
    Path(c.config_path).write_text("spec:\n  runtime: apptainer\n")
    assert a2a_sidecar.start_sidecar(c) is None


def test_start_sidecar_spawns_with_expected_argv(tmp_path):
    c = _make_config(tmp_path)
    fake_proc = MagicMock(pid=4242)
    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.subprocess.Popen",
        return_value=fake_proc,
    ) as mpopen:
        pid = a2a_sidecar.start_sidecar(c)
    assert pid == 4242
    argv = mpopen.call_args[0][0]
    assert argv[1:5] == ["-m", "scitex_agent_container", "a2a", "serve"]
    assert argv[5] == c.config_path
    assert "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8123"
    assert "--handler" in argv and argv[argv.index("--handler") + 1] == "echo"
    assert "-v" in argv
    # PID file written
    assert (Path(c.expanded_workdir) / "a2a-sidecar.pid").read_text() == "4242"


def test_start_sidecar_skips_when_already_running(tmp_path, caplog):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("777")

    with (
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar._process_alive",
            return_value=True,
        ),
        patch("scitex_agent_container.runtimes.a2a_sidecar.subprocess.Popen") as mpopen,
    ):
        with caplog.at_level("INFO"):
            pid = a2a_sidecar.start_sidecar(c)
    assert pid == 777
    mpopen.assert_not_called()


def test_start_sidecar_cleans_stale_pid_file(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("99999")  # stale
    fake_proc = MagicMock(pid=1234)

    with (
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar._process_alive",
            return_value=False,
        ),
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar.subprocess.Popen",
            return_value=fake_proc,
        ),
    ):
        pid = a2a_sidecar.start_sidecar(c)
    assert pid == 1234
    assert pid_path.read_text() == "1234"


def test_start_sidecar_handles_corrupt_pid_file(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not-a-number")
    fake_proc = MagicMock(pid=10)

    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.subprocess.Popen",
        return_value=fake_proc,
    ):
        pid = a2a_sidecar.start_sidecar(c)
    assert pid == 10


def test_start_sidecar_returns_none_on_spawn_oserror(tmp_path, caplog):
    c = _make_config(tmp_path)
    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar.subprocess.Popen",
        side_effect=OSError("no exec"),
    ):
        with caplog.at_level("WARNING"):
            pid = a2a_sidecar.start_sidecar(c)
    assert pid is None
    assert any("spawn failed" in r.message for r in caplog.records)


# --- stop_sidecar -------------------------------------------------------


def test_stop_sidecar_no_pid_file_is_noop(tmp_path):
    c = _make_config(tmp_path)
    assert a2a_sidecar.stop_sidecar(c) is False


def test_stop_sidecar_kills_live_process(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("321")

    with (
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar._process_alive",
            return_value=True,
        ),
        patch("scitex_agent_container.runtimes.a2a_sidecar.os.kill") as mk,
    ):
        assert a2a_sidecar.stop_sidecar(c) is True
    mk.assert_called_once_with(321, signal.SIGTERM)
    assert not pid_path.exists()


def test_stop_sidecar_kill_failure_logged_but_completes(tmp_path, caplog):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("321")

    with (
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar._process_alive",
            return_value=True,
        ),
        patch(
            "scitex_agent_container.runtimes.a2a_sidecar.os.kill",
            side_effect=OSError("EPERM"),
        ),
    ):
        with caplog.at_level("WARNING"):
            assert a2a_sidecar.stop_sidecar(c) is True
    # PID file still removed (cleanup path).
    assert not pid_path.exists()


def test_stop_sidecar_dead_pid_cleans_file_and_returns_false(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("777")

    with patch(
        "scitex_agent_container.runtimes.a2a_sidecar._process_alive",
        return_value=False,
    ):
        assert a2a_sidecar.stop_sidecar(c) is False
    assert not pid_path.exists()


def test_stop_sidecar_corrupt_pid_returns_false(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not-a-number")

    assert a2a_sidecar.stop_sidecar(c) is False
    assert not pid_path.exists()


def test_stop_sidecar_negative_pid_returns_false(tmp_path):
    c = _make_config(tmp_path)
    pid_path = Path(c.expanded_workdir) / "a2a-sidecar.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("-1")

    assert a2a_sidecar.stop_sidecar(c) is False
    assert not pid_path.exists()
