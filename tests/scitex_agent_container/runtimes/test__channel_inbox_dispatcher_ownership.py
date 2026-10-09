"""Ownership proof for the channel inbox dispatcher pidfile.

Covers the lifecycle rootfix: realpath config comparison, registry-known
spellings, prior-version owner acceptance, dead-pidfile reaping, DB
incarnation reconciliation before refusal, structured refusal codes,
and the owner heartbeat.

AAA markers per test, behaviour-shaped names, one assertion per test.
No mocks — fake ``/proc`` trees and injected seams only.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._acl_types import CommsSpec
from scitex_agent_container.config._types import A2ASpec
from scitex_agent_container.runtimes import (
    _channel_inbox_dispatcher_lifecycle as lifecycle,
)
from scitex_agent_container.runtimes import (
    _channel_inbox_dispatcher_ownership as ownership,
)
from scitex_agent_container.runtimes._inbox_sidecar_reconcile import (
    LEGACY_MODULE,
)


def _config(tmp_path: Path, name: str = "scholar") -> AgentConfig:
    return AgentConfig(
        name=name,
        harness="hermes",
        runtime="tui",
        a2a=A2ASpec(port=19001),
        comms=CommsSpec(channels=["server:sac", "server:scitex-cards"]),
        config_path=str(tmp_path / name / "spec.yaml"),
    )


def _fake_proc(
    tmp_path: Path,
    pid: int,
    *,
    module: str,
    name: str,
    config_path: str,
) -> Path:
    proc_root = tmp_path / "proc"
    cmdline = proc_root / str(pid) / "cmdline"
    cmdline.parent.mkdir(parents=True, exist_ok=True)
    cmdline.write_bytes(
        b"python\0-m\0"
        + module.encode()
        + b"\0--name\0"
        + name.encode()
        + b"\0--config-path\0"
        + config_path.encode()
        + b"\0"
    )
    return proc_root


def _write_legacy_pidfile(state_dir: Path, pid: int) -> Path:
    path = state_dir / lifecycle.PID_FILENAME
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def _detailed(
    config: AgentConfig,
    state_dir: Path,
    pid: int,
    *,
    owns_result: bool,
    row: dict | None,
    pid_alive: bool | None = True,
    row_pid_alive: bool | None = True,
) -> tuple:
    signals: list = []
    written: list = []

    def owns(_pid: int, **_kwargs: object) -> bool:
        return owns_result

    def reader(_name: str) -> dict | None:
        return row

    def writer(row_id: str, reason: str) -> bool:
        written.append((row_id, reason))
        return True

    def alive(probe: int) -> bool | None:
        if probe == pid:
            return pid_alive
        return row_pid_alive

    # Act
    outcome = ownership.stop_inbox_dispatcher_detailed(
        config,
        kill=lambda _pid, _sig: signals.append((_pid, _sig)),
        sleep=lambda _seconds: None,
        state_dir=state_dir,
        owns=owns,
        instance_reader=reader,
        stop_writer=writer,
        pid_alive_fn=alive,
        known_config_paths=[],
    )
    return outcome, signals, written


def test_ownership_accepts_symlinked_config_spelling(tmp_path):
    # Arrange — the live process was launched via the real path while the
    # authority spelling is a symlink to the same file.
    real = tmp_path / "real" / "spec.yaml"
    real.parent.mkdir(parents=True)
    real.write_text("name: scholar\n", encoding="utf-8")
    link = tmp_path / "linked.yaml"
    link.symlink_to(real)
    proc_root = _fake_proc(
        tmp_path,
        42,
        module=lifecycle.MODULE_PATH,
        name="scholar",
        config_path=str(real),
    )
    # Act
    owned = ownership._owns_dispatcher_process(
        42, name="scholar", config_path=str(link), proc_root=proc_root
    )
    # Assert
    assert owned is True


def test_ownership_rejects_different_config_file(tmp_path):
    # Arrange — same agent name, but the process serves another spec file.
    other = tmp_path / "other" / "spec.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("name: scholar\n", encoding="utf-8")
    mine = tmp_path / "mine" / "spec.yaml"
    mine.parent.mkdir(parents=True)
    mine.write_text("name: scholar\n", encoding="utf-8")
    proc_root = _fake_proc(
        tmp_path,
        42,
        module=lifecycle.MODULE_PATH,
        name="scholar",
        config_path=str(other),
    )
    # Act
    owned = ownership._owns_dispatcher_process(
        42, name="scholar", config_path=str(mine), proc_root=proc_root
    )
    # Assert
    assert owned is False


def test_ownership_accepts_prior_version_module(tmp_path):
    # Arrange — a dispatcher launched by the pre-rename generation.
    spec = tmp_path / "spec.yaml"
    spec.write_text("name: scholar\n", encoding="utf-8")
    proc_root = _fake_proc(
        tmp_path,
        42,
        module=LEGACY_MODULE,
        name="scholar",
        config_path=str(spec),
    )
    # Act
    owned = ownership._owns_dispatcher_process(
        42, name="scholar", config_path=str(spec), proc_root=proc_root
    )
    # Assert — accepted (with warning at the call site), never refused.
    assert owned is True


def test_ownership_rejects_foreign_agent_name(tmp_path):
    # Arrange — a recycled PID now serves a different agent.
    spec = tmp_path / "spec.yaml"
    spec.write_text("name: scholar\n", encoding="utf-8")
    proc_root = _fake_proc(
        tmp_path,
        42,
        module=lifecycle.MODULE_PATH,
        name="writer",
        config_path=str(spec),
    )
    # Act
    owned = ownership._owns_dispatcher_process(
        42, name="scholar", config_path=str(spec), proc_root=proc_root
    )
    # Assert
    assert owned is False


def test_stamped_pidfile_round_trip_preserves_identity(tmp_path):
    # Arrange
    path = tmp_path / lifecycle.PID_FILENAME
    # Act
    ownership.write_dispatcher_pidfile(
        path, 4242, incarnation_id="inc-1", module=lifecycle.MODULE_PATH
    )
    record = ownership.read_dispatcher_pidfile(path)
    # Assert
    assert record is not None and (
        record.pid,
        record.incarnation_id,
        record.module,
        record.legacy,
        record.sac_version is not None,
    ) == (4242, "inc-1", lifecycle.MODULE_PATH, False, True)


def test_legacy_plain_pidfile_reads_as_prior_version_owner(tmp_path):
    # Arrange — a pre-stamp pidfile carries a bare pid only.
    path = _write_legacy_pidfile(tmp_path, 4242)
    # Act
    record = ownership.read_dispatcher_pidfile(path)
    # Assert
    assert record is not None and (
        record.pid,
        record.sac_version,
        record.legacy,
    ) == (4242, None, True)


def test_stop_reaps_dead_pid_without_signalling(tmp_path):
    # Arrange — the recorded pid is demonstrably dead.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ownership.write_dispatcher_pidfile(
        state_dir / lifecycle.PID_FILENAME,
        4242,
        incarnation_id="inc-1",
        module=lifecycle.MODULE_PATH,
    )
    # Act
    outcome, signals, _written = _detailed(
        config, state_dir, 4242, owns_result=False, row=None, pid_alive=False
    )
    # Assert — reaped and proceeded, nothing signalled.
    assert (
        outcome.stopped,
        outcome.code,
        signals,
        (state_dir / lifecycle.PID_FILENAME).exists(),
    ) == (True, ownership.STOP_CODE_REAPED, [], False)


def test_stop_refuses_live_foreign_owner_without_signalling(tmp_path):
    # Arrange — live pid, argv mismatch, incarnation row active + live.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    row = {"id": "row-1", "name": "scholar", "pid": os.getpid(),
           "ended_at": None}
    # Act
    outcome, signals, _written = _detailed(
        config, state_dir, os.getpid(), owns_result=False, row=row,
        pid_alive=True, row_pid_alive=True,
    )
    # Assert — refused with the structured foreign-owner code.
    assert (outcome.stopped, outcome.code, signals) == (
        False,
        ownership.STOP_CODE_REFUSED_FOREIGN,
        [],
    )


def test_stop_proceeds_when_incarnation_row_ended(tmp_path):
    # Arrange — argv mismatch, but the incarnation row is already ended.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    row = {"id": "row-1", "name": "scholar", "pid": os.getpid(),
           "ended_at": "2026-10-09T00:00:00Z", "exit_reason": "stopped"}
    # Act
    outcome, signals, _written = _detailed(
        config, state_dir, os.getpid(), owns_result=False, row=row,
        pid_alive=True, row_pid_alive=True,
    )
    # Assert — stale pointer reaped, stop proceeds.
    assert (
        outcome.stopped,
        outcome.code,
        signals,
        (state_dir / lifecycle.PID_FILENAME).exists(),
    ) == (True, ownership.STOP_CODE_SUPERSEDED, [], False)


def test_stop_marks_stale_row_and_proceeds(tmp_path):
    # Arrange — argv mismatch, row active, but the row's own pid is dead.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    row = {"id": "row-1", "name": "scholar", "pid": 4242, "ended_at": None}
    # Act
    outcome, _signals, written = _detailed(
        config, state_dir, os.getpid(), owns_result=False, row=row,
        pid_alive=True, row_pid_alive=False,
    )
    # Assert — the stale row is closed with the stale-cleared reason.
    assert (outcome.stopped, written) == (True, [("row-1", "stale-cleared")])


def test_stop_refuses_when_row_unknown(tmp_path):
    # Arrange — argv mismatch and no incarnation row to consult.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    # Act
    outcome, signals, _written = _detailed(
        config, state_dir, os.getpid(), owns_result=False, row=None,
        pid_alive=True,
    )
    # Assert — unverifiable owners refuse, never signal.
    assert (outcome.stopped, outcome.code, signals) == (
        False,
        ownership.STOP_CODE_REFUSED_UNVERIFIED,
        [],
    )


def test_stop_signals_proven_owner_with_sigterm_first(tmp_path):
    # Arrange — ownership proven, then the process exits on SIGTERM.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    signals: list = []
    calls = {"count": 0}

    def owns(_pid: int, **_kwargs: object) -> bool:
        calls["count"] += 1
        return calls["count"] < 3

    # Act
    outcome = ownership.stop_inbox_dispatcher_detailed(
        config,
        kill=lambda _pid, _sig: signals.append((_pid, _sig)),
        sleep=lambda _seconds: None,
        state_dir=state_dir,
        owns=owns,
        instance_reader=lambda _name: None,
        stop_writer=lambda _rid, _reason: False,
        pid_alive_fn=lambda _pid: True,
        known_config_paths=[],
    )
    # Assert — SIGTERM only, no SIGKILL escalation for a clean exit.
    assert (outcome.stopped, outcome.code, signals) == (
        True,
        ownership.STOP_CODE_STOPPED,
        [(os.getpid(), signal.SIGTERM)],
    )


def test_stop_accepts_registry_known_spelling(tmp_path):
    # Arrange — the live spelling matches only the registry spelling.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_legacy_pidfile(state_dir, os.getpid())
    seen: list = []

    def owns(_pid: int, *, name: str, config_path: str) -> bool:
        seen.append(config_path)
        return config_path == "/authority/scholar/spec.yaml"

    # Act
    outcome = ownership.stop_inbox_dispatcher_detailed(
        config,
        kill=lambda _pid, _sig: seen.append("signalled"),
        sleep=lambda _seconds: None,
        state_dir=state_dir,
        owns=owns,
        instance_reader=lambda _name: None,
        stop_writer=lambda _rid, _reason: False,
        pid_alive_fn=lambda _pid: True,
        known_config_paths=["/authority/scholar/spec.yaml"],
    )
    # Assert — a spelling the registry knows is proof enough to proceed.
    assert (outcome.stopped, "signalled" in seen) == (True, True)


def test_touch_updates_pidfile_mtime(tmp_path):
    # Arrange — a pidfile gone stale.
    path = tmp_path / lifecycle.PID_FILENAME
    path.write_text("4242\n", encoding="utf-8")
    os.utime(path, (time.time() - 1000, time.time() - 1000))
    before = path.stat().st_mtime_ns
    # Act
    touched = ownership.touch_dispatcher_pidfile(path)
    # Assert
    assert (touched, path.stat().st_mtime_ns > before) == (True, True)


def test_pidfile_expired_flags_stale_mtime(tmp_path):
    # Arrange
    path = tmp_path / lifecycle.PID_FILENAME
    path.write_text("4242\n", encoding="utf-8")
    os.utime(path, (time.time() - 1000, time.time() - 1000))
    # Act
    expired = ownership.pidfile_expired(path, ttl_s=10.0)
    # Assert
    assert expired is True


def test_pidfile_fresh_mtime_not_expired(tmp_path):
    # Arrange
    path = tmp_path / lifecycle.PID_FILENAME
    path.write_text("4242\n", encoding="utf-8")
    # Act
    expired = ownership.pidfile_expired(path, ttl_s=600.0)
    # Assert
    assert expired is False


def test_serve_heartbeats_while_consuming(tmp_path):
    # Arrange — a pidfile with a stale mtime and a short-lived consumer.
    from scitex_agent_container.runtimes import (
        _channel_inbox_dispatcher as dispatcher,
    )

    pid_path = tmp_path / lifecycle.PID_FILENAME
    pid_path.write_text("4242\n", encoding="utf-8")
    os.utime(pid_path, (time.time() - 1000, time.time() - 1000))
    before = pid_path.stat().st_mtime_ns

    async def consume_briefly(**_kwargs: object) -> None:
        await asyncio.sleep(0.05)

    # Act
    asyncio.run(
        dispatcher._serve(
            consume_fn=consume_briefly,
            consume_kwargs={},
            pid_path=pid_path,
            heartbeat_s=0.01,
        )
    )
    # Assert — the owner ticked the mtime while consuming.
    assert pid_path.stat().st_mtime_ns > before


def test_dispatcher_running_reaps_dead_pidfile(tmp_path):
    # Arrange — the pidfile names a demonstrably dead pid.
    config = _config(tmp_path)
    state_dir = tmp_path / "scholar" / "state"
    state_dir.mkdir(parents=True)
    ownership.write_dispatcher_pidfile(
        state_dir / lifecycle.PID_FILENAME,
        4242,
        incarnation_id="inc-1",
        module=lifecycle.MODULE_PATH,
    )
    real_state = lifecycle.state_dir_for_config
    real_alive = lifecycle._pid_alive
    lifecycle.state_dir_for_config = lambda _config: state_dir
    lifecycle._pid_alive = lambda _pid: False
    try:
        # Act
        running = lifecycle.dispatcher_running(
            config,
            known_config_paths=[],
        )
    finally:
        lifecycle.state_dir_for_config = real_state
        lifecycle._pid_alive = real_alive
    # Assert — dead pointer reads as not-running and is reaped.
    assert (running, (state_dir / lifecycle.PID_FILENAME).exists()) == (
        False,
        False,
    )
