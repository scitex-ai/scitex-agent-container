"""Tests for :mod:`scitex_agent_container.runtimes.a2a_sidecar` (PA-306).

No-mocks: real PID files on disk, real ``subprocess.Popen`` of either
``sleep`` (for lifecycle bookkeeping paths) or the *real*
``python -m scitex_agent_container a2a serve`` CLI (for the happy-path
start), real socket binds to claim free ports, real ``os.kill`` for
liveness probes and SIGTERM.

Each test follows the AAA shape with a single, narrow assert.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.a2a_sidecar import (
    LOG_FILENAME,
    PID_FILENAME,
    _process_alive,
    _read_a2a_block,
    start_sidecar,
    stop_sidecar,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Claim and immediately release a localhost port via a real socket bind."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_pid_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_pid_alive(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _write_yaml(path: Path, *, port: int | None, handler: str = "echo") -> Path:
    """Write a minimal v3 agent YAML with the requested ``spec.a2a`` block."""
    doc: dict = {
        "apiVersion": "scitex/v3",
        "kind": "Agent",
        "metadata": {"name": "sidecar-test"},
        "spec": {},
    }
    if port is not None:
        doc["spec"]["a2a"] = {
            "port": port,
            "host": "127.0.0.1",
            "handler": handler,
        }
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


def _config(
    workdir: Path,
    *,
    config_path: str = "",
) -> AgentConfig:
    return AgentConfig(
        name="alpha",
        runtime="apptainer",
        workdir=str(workdir),
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# _read_a2a_block (pure-parse paths)
# ---------------------------------------------------------------------------


def test_read_a2a_block_returns_none_when_config_path_unset(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir, config_path="")
    # Act
    result = _read_a2a_block(cfg)
    # Assert
    assert result is None


def test_read_a2a_block_returns_none_when_yaml_missing(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    cfg = _config(workdir, config_path=str(tmp_path / "does-not-exist.yaml"))
    # Act
    result = _read_a2a_block(cfg)
    # Assert
    assert result is None


def test_read_a2a_block_returns_none_when_a2a_missing(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=None)
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    result = _read_a2a_block(cfg)
    # Assert
    assert result is None


def test_read_a2a_block_returns_none_for_malformed_yaml(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(":\n  - not: [valid\n")
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    result = _read_a2a_block(cfg)
    # Assert
    assert result is None


def test_read_a2a_block_returns_dict_when_port_present(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=9999, handler="echo")
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    result = _read_a2a_block(cfg)
    # Assert
    assert result == {"port": 9999, "host": "127.0.0.1", "handler": "echo"}


# ---------------------------------------------------------------------------
# _process_alive (real PIDs)
# ---------------------------------------------------------------------------


def test_process_alive_true_for_live_subprocess() -> None:
    # Arrange
    proc = subprocess.Popen(["sleep", "30"])
    try:
        # Act
        result = _process_alive(proc.pid)
        # Assert
        assert result is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_process_alive_false_for_reaped_pid() -> None:
    # Arrange
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    # Act
    result = _process_alive(proc.pid)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# start_sidecar
# ---------------------------------------------------------------------------


def test_start_sidecar_noop_when_a2a_disabled(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir, config_path="")
    # Act
    pid = start_sidecar(cfg)
    # Assert
    assert pid is None


def test_start_sidecar_returns_existing_pid_when_already_running(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: real sleep process + real PID file pre-populated.
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path))
    sleeper = subprocess.Popen(["sleep", "30"])
    (workdir / PID_FILENAME).write_text(str(sleeper.pid))
    try:
        # Act
        returned = start_sidecar(cfg)
        # Assert
        assert returned == sleeper.pid
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_start_sidecar_spawns_real_subprocess_and_writes_pid_file(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: claim+release a free port; real CLI will rebind it.
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    pid = start_sidecar(cfg)
    try:
        # Wait for the real subprocess to come up enough that os.kill(pid, 0)
        # succeeds — this exercises the production write-then-spawn ordering.
        _wait_pid_alive(pid, timeout=5.0)
        pid_on_disk = int((workdir / PID_FILENAME).read_text().strip())
        # Assert: on-disk PID matches the value returned by start_sidecar.
        assert pid_on_disk == pid
    finally:
        if pid is not None and _process_alive(pid):
            os.kill(pid, 15)  # SIGTERM
            _wait_pid_dead(pid, timeout=5.0)
        (workdir / PID_FILENAME).unlink(missing_ok=True)


def test_start_sidecar_creates_log_file(workdir: Path, tmp_path: Path) -> None:
    # Arrange
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    pid = start_sidecar(cfg)
    try:
        # Assert: log file is opened for append by start_sidecar itself,
        # so it exists regardless of whether the child has written yet.
        assert (workdir / LOG_FILENAME).exists()
    finally:
        if pid is not None and _process_alive(pid):
            os.kill(pid, 15)
            _wait_pid_dead(pid, timeout=5.0)


def test_start_sidecar_clears_stale_pid_file_and_respawns(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: PID file points to a dead PID; start_sidecar must clear it and
    # spawn a fresh sidecar with a new PID.
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    dead_pid = proc.pid
    (workdir / PID_FILENAME).write_text(str(dead_pid))

    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path))
    # Act
    new_pid = start_sidecar(cfg)
    try:
        # Assert: a different, live PID was spawned.
        assert new_pid is not None and new_pid != dead_pid
    finally:
        if new_pid is not None and _process_alive(new_pid):
            os.kill(new_pid, 15)
            _wait_pid_dead(new_pid, timeout=5.0)


# ---------------------------------------------------------------------------
# stop_sidecar
# ---------------------------------------------------------------------------


def test_stop_sidecar_returns_false_when_pid_file_absent(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir)
    # Act
    result = stop_sidecar(cfg)
    # Assert
    assert result is False


def test_stop_sidecar_clears_pid_file_with_garbage_contents(workdir: Path) -> None:
    # Arrange
    (workdir / PID_FILENAME).write_text("not-a-pid\n")
    cfg = _config(workdir)
    # Act
    result = stop_sidecar(cfg)
    # Assert
    assert result is False and not (workdir / PID_FILENAME).exists()


def test_stop_sidecar_clears_pid_file_when_process_already_dead(
    workdir: Path,
) -> None:
    # Arrange: reaped PID — _process_alive returns False.
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    (workdir / PID_FILENAME).write_text(str(proc.pid))
    cfg = _config(workdir)
    # Act
    result = stop_sidecar(cfg)
    # Assert
    assert result is False and not (workdir / PID_FILENAME).exists()


def test_stop_sidecar_kills_live_process_and_removes_pid_file(
    workdir: Path,
) -> None:
    # Arrange: real long-running sleep process registered via PID file.
    sleeper = subprocess.Popen(["sleep", "30"])
    (workdir / PID_FILENAME).write_text(str(sleeper.pid))
    cfg = _config(workdir)
    try:
        # Act
        result = stop_sidecar(cfg)
        sleeper.wait(timeout=5)
        # Assert
        assert result is True and not (workdir / PID_FILENAME).exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
