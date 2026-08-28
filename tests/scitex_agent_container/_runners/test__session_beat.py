#!/usr/bin/env python3
"""The diary is telemetry: an unreachable store must cost rows, never the agent.

WHY THESE ASSERTIONS AND NOT OTHERS. ``_DefaultDBWriter`` has promised
"best-effort ... we catch + log here" in its docstring since it was written,
but nothing caught anything. Under SQLite that was invisible — the diary
wrote to a local file that effectively never failed, so the un-implemented
promise was never exercised. Moving the diary to PostgreSQL made the write
genuinely failable, and every runner test that merely ticks a heartbeat died
on a refused connection. So the assertions here are about the two halves of
that contract, and they pull in opposite directions:

  * the runner SURVIVES a dead diary (otherwise telemetry can kill an agent), and
  * the failure is LOUD (otherwise we rebuild the defect we just fixed — the
    store DSN pointed at a read-only replica from 08-23 to 08-27 and nobody
    noticed, precisely because nothing complained about the writes it lost).

A test for only the first half would pass on an implementation that swallowed
every failure in silence, which is the wrong fix. Hence the log assertion is a
peer of the survival assertion, not a nicety.

NO MONKEYPATCH (PA-306 §3): env is saved and restored directly, and the
runner is exercised through its real entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from scitex_agent_container._runners import _session_beat as beat
from scitex_agent_container._runners import claude_session as runner

# Port 1 is privileged and never listening; the database name says out loud
# what a row arriving there would mean.
DEAD_DSN = "postgresql://sac_tests@127.0.0.1:1/tests_must_not_write_to_the_fleet_store"


@contextlib.contextmanager
def _dead_store():
    """Point the store at an unreachable DSN, then restore."""
    saved = os.environ.get("SCITEX_STORE_DSN")
    os.environ["SCITEX_STORE_DSN"] = DEAD_DSN
    beat._DIARY_FAILURES.clear()  # module-level counter; don't inherit a neighbour's
    try:
        yield
    finally:
        beat._DIARY_FAILURES.clear()
        if saved is None:
            os.environ.pop("SCITEX_STORE_DSN", None)
        else:
            os.environ["SCITEX_STORE_DSN"] = saved


# ---------------------------------------------------------------------------
# _DefaultDBWriter — absorbs the failure
# ---------------------------------------------------------------------------


def test_record_heartbeat_returns_none_when_the_store_is_unreachable() -> None:
    # Arrange
    with _dead_store():
        writer = beat._DefaultDBWriter()
        # Act
        result = writer.record_heartbeat(
            name="ag-beat-1", host="h", pid=1, state=beat.STATE_READY, ts="2026-08-28T00:00:00Z"
        )
    # Assert
    assert result is None


def test_record_turn_returns_none_when_the_store_is_unreachable() -> None:
    # Arrange
    with _dead_store():
        writer = beat._DefaultDBWriter()
        # Act
        result = writer.record_turn(
            turn_id="t-1",
            name="ag-beat-2",
            host="h",
            status="queued",
            prompt_text=None,
            response_text=None,
            session_id=None,
            input_tokens=None,
            output_tokens=None,
        )
    # Assert
    assert result is None


def test_record_error_returns_none_when_the_store_is_unreachable() -> None:
    # Arrange
    with _dead_store():
        writer = beat._DefaultDBWriter()
        # Act
        result = writer.record_error(
            name="ag-beat-3", host="h", cause="sdk-crash", detail=None, turn_id=None
        )
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# ... but says so. The negative control on "best-effort" meaning "silent".
# ---------------------------------------------------------------------------


def test_a_dropped_diary_row_is_logged_as_a_warning(caplog) -> None:
    # Arrange
    with _dead_store(), caplog.at_level(logging.WARNING):
        writer = beat._DefaultDBWriter()
        # Act
        writer.record_heartbeat(
            name="ag-beat-4", host="h", pid=1, state=beat.STATE_READY, ts="2026-08-28T00:00:00Z"
        )
    # Assert
    assert any("diary heartbeat write failed" in r.getMessage() for r in caplog.records)


def test_the_warning_carries_the_consecutive_failure_count(caplog) -> None:
    # Arrange — a diary down for a long time must be readable as such, not as
    # one indistinguishable line repeated.
    with _dead_store(), caplog.at_level(logging.WARNING):
        writer = beat._DefaultDBWriter()
        # Act
        for _ in range(beat._DIARY_WARN_EVERY):
            writer.record_heartbeat(
                name="ag-beat-5", host="h", pid=1, state=beat.STATE_READY, ts="2026-08-28T00:00:00Z"
            )
        messages = [r.getMessage() for r in caplog.records]
    # Assert
    assert any(f"{beat._DIARY_WARN_EVERY} consecutive" in m for m in messages)


def test_a_flooding_failure_does_not_log_once_per_call(caplog) -> None:
    # Arrange
    with _dead_store(), caplog.at_level(logging.WARNING):
        writer = beat._DefaultDBWriter()
        # Act
        for _ in range(beat._DIARY_WARN_EVERY):
            writer.record_heartbeat(
                name="ag-beat-6", host="h", pid=1, state=beat.STATE_READY, ts="2026-08-28T00:00:00Z"
            )
        dropped = [r for r in caplog.records if "diary heartbeat write failed" in r.getMessage()]
    # Assert — first failure plus the Nth, not one per beat.
    assert len(dropped) == 2


def test_each_row_kind_warns_on_its_own_first_failure(caplog) -> None:
    # Arrange — a broken heartbeat table must not mute the first turn failure.
    with _dead_store(), caplog.at_level(logging.WARNING):
        writer = beat._DefaultDBWriter()
        # Act
        writer.record_heartbeat(
            name="ag-beat-7", host="h", pid=1, state=beat.STATE_READY, ts="2026-08-28T00:00:00Z"
        )
        writer.record_error(
            name="ag-beat-7", host="h", cause="sdk-crash", detail=None, turn_id=None
        )
        messages = [r.getMessage() for r in caplog.records]
    # Assert
    assert any("diary heartbeat write failed" in m for m in messages)
    assert any("diary error write failed" in m for m in messages)


# ---------------------------------------------------------------------------
# The regression itself: the runner outlives its diary.
# ---------------------------------------------------------------------------


def test_run_exits_zero_when_the_diary_store_is_unreachable(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run("ag-beat-run", state_root=tmp_path, tick_seconds=0.01)

    # Act
    with _dead_store():
        rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0


def test_run_still_writes_the_heartbeat_file_when_the_diary_is_unreachable(
    tmp_path: Path,
) -> None:
    # Arrange — the FILE heartbeat is the local liveness signal and has no
    # dependency on Postgres; losing the diary must not take it down too.
    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run("ag-beat-run-2", state_root=tmp_path, tick_seconds=0.01)

    # Act
    with _dead_store():
        asyncio.run(_scenario())
    hb = runner.read_heartbeat(tmp_path / "ag-beat-run-2")
    # Assert
    assert hb is not None and hb["state"] == runner.STATE_STOPPING
