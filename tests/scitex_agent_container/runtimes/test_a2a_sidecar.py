"""Tests for :mod:`scitex_agent_container.runtimes.a2a_sidecar` (PA-306).

No-mocks: real PID files on disk, real ``subprocess.Popen`` of either
``sleep`` (for lifecycle bookkeeping paths) or the *real*
``python -m scitex_agent_container a2a serve`` CLI (for the happy-path
start), real socket binds to claim free ports, real ``os.kill`` for
liveness probes and SIGTERM.

Each test follows the AAA shape with a single, narrow assert.
"""

from __future__ import annotations

import functools
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.a2a_sidecar import (
    LOG_FILENAME,
    PID_FILENAME,
    _log_path,
    _pid_path,
    _proc_argv,
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


def _marked_sleeper(marker: Path) -> subprocess.Popen:
    """A live process whose KERNEL argv carries ``marker``.

    ``python -c ... <marker>`` rather than ``sh -c``: a shell tail-execs
    its single command and the extra argv word is lost with it, which
    would silently defeat the ownership probe this stands in for.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(marker)]
    )


def _kill(pid: int | None) -> None:
    if pid is not None and _process_alive(pid):
        os.kill(pid, 15)  # SIGTERM
        _wait_pid_dead(pid, timeout=5.0)


def _write_yaml(
    path: Path, *, port: int | None, handler: str = "echo", name: str = "sidecar-test"
) -> Path:
    """Write a minimal v3 agent YAML with the requested ``spec.a2a`` block."""
    doc: dict = {
        "apiVersion": "scitex/v3",
        "kind": "Agent",
        "metadata": {"name": name},
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
    name: str = "alpha",
) -> AgentConfig:
    return AgentConfig(
        name=name,
        runtime="apptainer",
        workdir=str(workdir),
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# Per-agent keying of the pid/log pair
#
# The defect: pid + log used to live at ``{workdir}/a2a-sidecar.*`` with no
# agent identity in the path, so two agents sharing a workdir shared one
# sidecar and the second start silently returned the FIRST agent's pid.
# ---------------------------------------------------------------------------


def test_pid_path_differs_per_agent_in_one_shared_workdir(workdir: Path) -> None:
    # Arrange: two agents, ONE workdir — the clone / twin / relocate shape.
    cfg_a = _config(workdir, name="shared-wd-alpha")
    cfg_b = _config(workdir, name="shared-wd-bravo")
    # Act
    paths = {_pid_path(cfg_a), _pid_path(cfg_b)}
    # Assert
    assert len(paths) == 2


def test_log_path_differs_per_agent_in_one_shared_workdir(workdir: Path) -> None:
    # Arrange
    cfg_a = _config(workdir, name="shared-wd-alpha")
    cfg_b = _config(workdir, name="shared-wd-bravo")
    # Act
    paths = {_log_path(cfg_a), _log_path(cfg_b)}
    # Assert
    assert len(paths) == 2


def test_pid_path_is_keyed_by_the_agent_name_directory(workdir: Path) -> None:
    # Arrange: identity belongs in the DIRECTORY, like every other per-agent
    # runtime artefact (pid / heartbeat.json / session.jsonl / runner.log).
    cfg = _config(workdir, name="dir-keyed-agent")
    # Act
    parent_name = _pid_path(cfg).parent.name
    # Assert
    assert parent_name == "dir-keyed-agent"


def test_pid_path_refuses_an_unnamed_agent(workdir: Path) -> None:
    # Arrange: a silent fallback to a shared path here would reproduce the bug.
    cfg = _config(workdir, name="")
    # Act
    resolve = functools.partial(_pid_path, cfg)
    # Assert
    with pytest.raises(ValueError, match="empty name"):
        resolve()


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
    cfg = _config(workdir, config_path=str(yaml_path), name="already-running-agent")
    pid_path = _pid_path(cfg)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    pid_path.write_text(str(sleeper.pid))
    try:
        # Act
        returned = start_sidecar(cfg)
        # Assert
        assert returned == sleeper.pid
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
        pid_path.unlink(missing_ok=True)


def test_start_sidecar_spawns_real_subprocess_and_writes_pid_file(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: claim+release a free port; real CLI will rebind it.
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="spawn-writes-pid-agent")
    # Act
    pid = start_sidecar(cfg)
    try:
        # Wait for the real subprocess to come up enough that os.kill(pid, 0)
        # succeeds — this exercises the production write-then-spawn ordering.
        _wait_pid_alive(pid, timeout=5.0)
        pid_on_disk = int(_pid_path(cfg).read_text().strip())
        # Assert: on-disk PID matches the value returned by start_sidecar.
        assert pid_on_disk == pid
    finally:
        _kill(pid)
        _pid_path(cfg).unlink(missing_ok=True)


def test_start_sidecar_creates_log_file(workdir: Path, tmp_path: Path) -> None:
    # Arrange
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="log-file-agent")
    # Act
    pid = start_sidecar(cfg)
    try:
        # Assert: log file is opened for append by start_sidecar itself,
        # so it exists regardless of whether the child has written yet.
        assert _log_path(cfg).exists()
    finally:
        _kill(pid)
        _pid_path(cfg).unlink(missing_ok=True)


def test_start_sidecar_clears_stale_pid_file_and_respawns(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: PID file points to a dead PID; start_sidecar must clear it and
    # spawn a fresh sidecar with a new PID.
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="stale-pid-agent")
    pid_path = _pid_path(cfg)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(dead_pid))
    # Act
    new_pid = start_sidecar(cfg)
    try:
        # Assert: a different, live PID was spawned.
        assert new_pid is not None and new_pid != dead_pid
    finally:
        _kill(new_pid)
        pid_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Two agents, ONE workdir — the reported defect, end to end with real sidecars
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_workdir_pair(tmp_path: Path):
    """Two REAL sidecars for two agents sharing ONE workdir.

    Function-scoped (each test gets its own pair) so no assertion can
    depend on another having run first.
    """
    root = tmp_path / "pair"
    root.mkdir()
    wd = root / "SHARED-WORKDIR"
    wd.mkdir()
    port_a, port_b = _free_port(), _free_port()
    spec_a = _write_yaml(root / "a.yaml", port=port_a, name="pair-alpha")
    spec_b = _write_yaml(root / "b.yaml", port=port_b, name="pair-bravo")
    cfg_a = _config(wd, config_path=str(spec_a), name="pair-alpha")
    cfg_b = _config(wd, config_path=str(spec_b), name="pair-bravo")

    pid_a = start_sidecar(cfg_a)
    pid_b = start_sidecar(cfg_b)
    _wait_pid_alive(pid_a, timeout=10.0)
    _wait_pid_alive(pid_b, timeout=10.0)
    yield {
        "cfg_a": cfg_a,
        "cfg_b": cfg_b,
        "pid_a": pid_a,
        "pid_b": pid_b,
        "port_b": port_b,
    }
    _kill(pid_a)
    _kill(pid_b)
    _pid_path(cfg_a).unlink(missing_ok=True)
    _pid_path(cfg_b).unlink(missing_ok=True)


def test_second_agent_in_a_shared_workdir_gets_its_own_pid(
    shared_workdir_pair: dict,
) -> None:
    # Arrange: pre-fix, the second start logged "already running" and handed
    # back the FIRST agent's pid.
    pair = shared_workdir_pair
    # Act
    pids = {pair["pid_a"], pair["pid_b"]}
    # Assert
    assert len(pids) == 2


def test_second_agent_in_a_shared_workdir_gets_its_own_pid_file(
    shared_workdir_pair: dict,
) -> None:
    # Arrange
    pair = shared_workdir_pair
    # Act
    recorded = {
        _pid_path(pair["cfg_a"]).read_text().strip(),
        _pid_path(pair["cfg_b"]).read_text().strip(),
    }
    # Assert: two files, two distinct recorded pids.
    assert len(recorded) == 2


def test_second_agents_recorded_pid_is_a_process_serving_its_own_port(
    shared_workdir_pair: dict,
) -> None:
    # Arrange: read the pid back off disk, then ask the KERNEL what that
    # process actually is — not the value start_sidecar returned.
    pair = shared_workdir_pair
    recorded_b = int(_pid_path(pair["cfg_b"]).read_text().strip())
    # Act
    argv = _proc_argv(recorded_b)
    # Assert
    assert str(pair["port_b"]) in argv


# ---------------------------------------------------------------------------
# Migration off the pre-keying ``{workdir}/a2a-sidecar.pid``
# ---------------------------------------------------------------------------


def test_start_sidecar_adopts_a_live_legacy_sidecar_of_ours(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: an agent already running when the keying change lands — its
    # sidecar is recorded ONLY at the legacy workdir path.
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="adopt-legacy-agent")
    legacy = workdir / PID_FILENAME
    incumbent = _marked_sleeper(yaml_path)
    legacy.write_text(str(incumbent.pid))
    try:
        # Act
        returned = start_sidecar(cfg)
        # Assert: adopted, not duplicated — no second bind attempt.
        assert returned == incumbent.pid
    finally:
        incumbent.terminate()
        incumbent.wait(timeout=5)
        _pid_path(cfg).unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)


def test_adopting_a_legacy_sidecar_moves_the_record_to_the_per_agent_path(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="adopt-moves-record-agent")
    legacy = workdir / PID_FILENAME
    incumbent = _marked_sleeper(yaml_path)
    legacy.write_text(str(incumbent.pid))
    try:
        # Act
        start_sidecar(cfg)
        # Assert
        assert _pid_path(cfg).read_text().strip() == str(incumbent.pid)
    finally:
        incumbent.terminate()
        incumbent.wait(timeout=5)
        _pid_path(cfg).unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)


def test_start_sidecar_ignores_a_legacy_pid_file_owned_by_another_agent(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: the legacy file names a LIVE process that belongs to a DIFFERENT
    # agent (its kernel argv carries the other agent's spec).
    other_yaml = _write_yaml(tmp_path / "other.yaml", port=_free_port())
    port = _free_port()
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=port)
    cfg = _config(workdir, config_path=str(yaml_path), name="ignore-stranger-agent")
    legacy = workdir / PID_FILENAME
    stranger = _marked_sleeper(other_yaml)
    legacy.write_text(str(stranger.pid))
    pid = None
    try:
        # Act
        pid = start_sidecar(cfg)
        # Assert: we spawned our OWN sidecar rather than adopting a stranger's.
        assert pid is not None and pid != stranger.pid
    finally:
        stranger.terminate()
        stranger.wait(timeout=5)
        _kill(pid)
        _pid_path(cfg).unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)


def test_stop_sidecar_terminates_a_live_legacy_sidecar_of_ours(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange: stopped (not restarted) right after the keying change — the
    # legacy sidecar would otherwise hold the port forever.
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=_free_port())
    cfg = _config(workdir, config_path=str(yaml_path), name="stop-legacy-agent")
    legacy = workdir / PID_FILENAME
    incumbent = _marked_sleeper(yaml_path)
    legacy.write_text(str(incumbent.pid))
    try:
        # Act
        stop_sidecar(cfg)
        # Assert: wait() reaps, so the exit code reports the signal that
        # killed it. (os.kill(pid, 0) still succeeds on an unreaped zombie,
        # which is why liveness alone would not be evidence here.)
        assert incumbent.wait(timeout=5) == -signal.SIGTERM
    finally:
        if incumbent.poll() is None:
            incumbent.kill()
            incumbent.wait(timeout=5)
        legacy.unlink(missing_ok=True)


def test_stop_sidecar_leaves_another_agents_legacy_sidecar_alive(
    workdir: Path, tmp_path: Path
) -> None:
    # Arrange
    other_yaml = _write_yaml(tmp_path / "other.yaml", port=_free_port())
    yaml_path = _write_yaml(tmp_path / "agent.yaml", port=_free_port())
    cfg = _config(workdir, config_path=str(yaml_path), name="stop-stranger-agent")
    legacy = workdir / PID_FILENAME
    stranger = _marked_sleeper(other_yaml)
    legacy.write_text(str(stranger.pid))
    try:
        # Act
        stop_sidecar(cfg)
        # Assert
        assert _process_alive(stranger.pid) is True
    finally:
        stranger.terminate()
        stranger.wait(timeout=5)
        legacy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# stop_sidecar
# ---------------------------------------------------------------------------


def test_stop_sidecar_returns_false_when_pid_file_absent(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir, name="stop-absent-agent")
    # Act
    result = stop_sidecar(cfg)
    # Assert
    assert result is False


def test_stop_sidecar_clears_pid_file_with_garbage_contents(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir, name="stop-garbage-agent")
    pid_path = _pid_path(cfg)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not-a-pid\n")
    # Act
    stop_sidecar(cfg)
    # Assert
    assert not pid_path.exists()


def test_stop_sidecar_clears_pid_file_when_process_already_dead(
    workdir: Path,
) -> None:
    # Arrange: reaped PID — _process_alive returns False.
    cfg = _config(workdir, name="stop-dead-agent")
    pid_path = _pid_path(cfg)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    pid_path.write_text(str(proc.pid))
    # Act
    stop_sidecar(cfg)
    # Assert
    assert not pid_path.exists()


def test_stop_sidecar_kills_live_process_and_removes_pid_file(
    workdir: Path,
) -> None:
    # Arrange: real long-running sleep process registered via PID file.
    cfg = _config(workdir, name="stop-live-agent")
    pid_path = _pid_path(cfg)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    pid_path.write_text(str(sleeper.pid))
    try:
        # Act
        result = stop_sidecar(cfg)
        sleeper.wait(timeout=5)
        # Assert
        assert result is True and not pid_path.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_log_filename_constant_is_the_sidecar_log_basename(workdir: Path) -> None:
    # Arrange
    cfg = _config(workdir, name="log-basename-agent")
    # Act
    basename = _log_path(cfg).name
    # Assert
    assert basename == LOG_FILENAME
