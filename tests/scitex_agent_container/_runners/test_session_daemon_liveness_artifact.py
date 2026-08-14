"""v4 step-5 liveness artifact, as the DAEMON emits it.

Pins the observable artifact of :mod:`session_daemon`: beats carrying
``{seq, writer, state, turns_completed, incarnation_id?}`` and the
terminal ExitRecord on the signal-stop and foreground-one-shot paths.
(The residency-violation exits — harness-returned / crashed — live in
``test_session_daemon_zombie_exit.py`` beside the fix they pin.)

No mocks — hand-rolled coroutines only, same as the sibling suites.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._incarnation import (
    EXIT_ONESHOT_COMPLETE,
    EXIT_STOPPED_BY_SIGNAL,
    WRITER_SESSION_DAEMON,
    read_exit_record,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)
from scitex_agent_container._runners._session_state import (
    STATE_READY,
    STATE_STOPPING,
    read_heartbeat,
)


async def _drain_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """Minimal well-behaved turn driver: ack every turn until shutdown.

    Mirrors the real ``run_conversation`` contract, including the
    ``exit_after`` handshake: a foreground one-shot's mission envelope
    ends the conversation (stop set, driver returns).
    """
    inbox = kwargs["inbox"]
    stop = kwargs["stop"]
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_result("ack")
        if isinstance(env, TurnEnvelope) and env.exit_after:
            stop.set()
            return


def _sigterm_soon(delay_s: float = 0.05) -> None:
    async def _kick() -> None:
        await asyncio.sleep(delay_s)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_kick())


def _run_signal_stop(tmp_path: Path, name: str, *, delay_s: float = 0.05) -> int:
    """Run a resident daemon and SIGTERM it; return the exit code."""

    async def _scenario() -> int:
        _sigterm_soon(delay_s)
        return await session_daemon.run_session_daemon(
            name,
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
        )

    return asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# the beat, as landed
# ---------------------------------------------------------------------------


def test_daemon_beats_carry_the_writer_name(tmp_path: Path) -> None:
    # Arrange
    # Act
    _run_signal_stop(tmp_path, "ag-writer")
    hb = read_heartbeat(tmp_path / "ag-writer")
    # Assert
    assert hb is not None and hb["writer"] == WRITER_SESSION_DAEMON


def test_daemon_beats_seq_is_monotonic(tmp_path: Path) -> None:
    # Arrange: several ticks elapse before the SIGTERM lands.
    # Act
    _run_signal_stop(tmp_path, "ag-seq", delay_s=0.1)
    hb = read_heartbeat(tmp_path / "ag-seq")
    # Assert: STARTING + ticked beats + STOPPING → seq advanced past 1.
    assert hb is not None and hb["seq"] > 1


def test_daemon_beats_carry_turns_completed(tmp_path: Path) -> None:
    # Arrange
    # Act
    _run_signal_stop(tmp_path, "ag-turns")
    hb = read_heartbeat(tmp_path / "ag-turns")
    # Assert: present from beat one (0 when no ResultMessage landed yet).
    assert hb is not None and hb["turns_completed"] == 0


def test_daemon_resident_beat_state_is_ready(tmp_path: Path) -> None:
    # Arrange: sample the beat MID-RUN from inside the driver, after the
    # loop's first tick has landed (the driver runs while resident).
    sampled: dict[str, Any] = {}

    async def _sampling_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
        await asyncio.sleep(0.05)
        sampled.update(read_heartbeat(state_dir) or {})
        await _drain_driver(name, state_dir, **kwargs)

    async def _scenario() -> int:
        _sigterm_soon(0.15)
        return await session_daemon.run_session_daemon(
            "ag-ready",
            turn_driver=_sampling_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
        )

    # Act
    asyncio.run(_scenario())
    # Assert: the resident state is READY — a live inbox consumer, not a
    # blanket "idle".
    assert sampled.get("state") == STATE_READY


def test_daemon_final_beat_is_stopping(tmp_path: Path) -> None:
    # Arrange
    # Act
    _run_signal_stop(tmp_path, "ag-stopbeat")
    hb = read_heartbeat(tmp_path / "ag-stopbeat")
    # Assert
    assert hb is not None and hb["state"] == STATE_STOPPING


def test_daemon_beat_stamps_bound_incarnation(tmp_path: Path) -> None:
    # Arrange: the start path published the instance_id marker (fresh
    # mtime) before the daemon boots — the daemon adopts it.
    state_dir = tmp_path / "ag-incarn"
    state_dir.mkdir(parents=True)
    (state_dir / "instance_id").write_text("inc-live-1", encoding="utf-8")
    # Act
    _run_signal_stop(tmp_path, "ag-incarn")
    hb = read_heartbeat(state_dir)
    # Assert
    assert hb is not None and hb["incarnation_id"] == "inc-live-1"


# ---------------------------------------------------------------------------
# the ExitRecord, on the planned-end paths
# ---------------------------------------------------------------------------


def test_signal_stop_writes_exit_record_reason(tmp_path: Path) -> None:
    # Arrange
    # Act
    _run_signal_stop(tmp_path, "ag-exitsig")
    rec = read_exit_record(tmp_path / "ag-exitsig")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_STOPPED_BY_SIGNAL


def test_signal_stop_exit_record_code_is_zero(tmp_path: Path) -> None:
    # Arrange
    # Act
    rc = _run_signal_stop(tmp_path, "ag-exitrc")
    rec = read_exit_record(tmp_path / "ag-exitrc")
    # Assert
    assert (rc, rec["code"]) == (0, 0)


def test_exit_record_cites_the_bound_incarnation(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "ag-exitinc"
    state_dir.mkdir(parents=True)
    (state_dir / "instance_id").write_text("inc-live-2", encoding="utf-8")
    # Act
    _run_signal_stop(tmp_path, "ag-exitinc")
    rec = read_exit_record(state_dir)
    # Assert
    assert rec is not None and rec["incarnation_id"] == "inc-live-2"


def test_daemon_boot_clears_predecessor_exit_record(tmp_path: Path) -> None:
    # Arrange: a previous incarnation's farewell sits in the state dir;
    # this generation must not present it as its own... and after ITS
    # exit, the record describes the NEW run (signal), not the old one.
    state_dir = tmp_path / "ag-clear"
    state_dir.mkdir(parents=True)
    from scitex_agent_container._runners._incarnation import write_exit_record

    write_exit_record(state_dir, reason="crashed", code=1)
    # Act
    _run_signal_stop(tmp_path, "ag-clear")
    rec = read_exit_record(state_dir)
    # Assert
    assert rec is not None and rec["reason"] == EXIT_STOPPED_BY_SIGNAL


def test_foreground_oneshot_exit_record_is_oneshot_complete(tmp_path: Path) -> None:
    # Arrange: --print-stream mission → the planned finite run.
    async def _scenario() -> int:
        return await session_daemon.run_session_daemon(
            "ag-oneshot",
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
        )

    # Act
    asyncio.run(_scenario())
    rec = read_exit_record(tmp_path / "ag-oneshot")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_ONESHOT_COMPLETE


def test_foreground_oneshot_returns_zero(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> int:
        return await session_daemon.run_session_daemon(
            "ag-oneshot0",
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0
