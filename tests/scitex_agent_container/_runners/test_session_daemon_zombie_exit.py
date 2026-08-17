"""The residency contract — a dead conversation ends the daemon.

Card sac-sdk-runner-stop-never-set-zombie-resident-20260814: nothing
set ``stop`` when the turn driver returned or died on its own, so the
daemon stayed parked on ``stop.wait()`` forever — a RESIDENT ZOMBIE
with green heartbeats, a bound a2a port, and no inbox consumer; every
incoming turn died at the 120s timeout while every liveness proxy read
alive. These tests pin the fix: the conversation task's completion
folds into ``stop`` (done-callback), the daemon EXITS with a non-zero
code, and the ExitRecord names the violation.

Each scenario is bounded by ``asyncio.wait_for`` — against pre-fix code
the daemon never returns and the test FAILS with TimeoutError (a
timeout, not a sleep-and-hope).

No mocks — hand-rolled coroutines only, same as the sibling suites.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._incarnation import (
    EXIT_CRASHED,
    EXIT_HARNESS_RETURNED,
    read_exit_record,
)
from scitex_agent_container._runners._session_inbox import TurnEnvelope

#: Generous ceiling for "the daemon must EXIT on its own". Pre-fix code
#: parks on stop.wait() forever, so this is what converts the zombie
#: into a visible test failure.
_EXIT_DEADLINE_S = 10.0


async def _returning_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """A driver that violates residency: answers one turn, then RETURNS."""
    inbox = kwargs["inbox"]
    env = await inbox.get()
    if isinstance(env, TurnEnvelope) and not env.response.done():
        env.response.set_result("ack")
    # ...and returns without ShutdownEnvelope, without setting stop —
    # the exact shape the 2026-08-14 canary investigation flagged.


async def _dying_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """A driver that dies outright (an unhandled harness exception)."""
    raise RuntimeError("harness fell over")


def _run_bounded(tmp_path: Path, name: str, driver: Any) -> int:
    """Run a RESIDENT daemon (mission, no print-stream) with a deadline."""

    async def _scenario() -> int:
        return await asyncio.wait_for(
            session_daemon.run_session_daemon(
                name,
                turn_driver=driver,
                state_root=tmp_path,
                tick_seconds=0.01,
                mission="boot",
            ),
            timeout=_EXIT_DEADLINE_S,
        )

    return asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# harness-returned: the driver returns while the daemon is resident
# ---------------------------------------------------------------------------


def test_daemon_exits_when_conversation_returns(tmp_path: Path) -> None:
    # Arrange: a resident daemon whose driver answers the mission turn
    # and then simply returns — the zombie shape.
    # Act: pre-fix this parks on stop.wait() forever and the wait_for
    # deadline fails the test; post-fix the daemon exits promptly.
    rc = _run_bounded(tmp_path, "ag-zombie", _returning_driver)
    # Assert: a residency violation is a FAILURE exit, not a clean 0.
    assert rc != 0


def test_conversation_return_writes_harness_returned_exit_record(
    tmp_path: Path,
) -> None:
    # Arrange
    # Act
    _run_bounded(tmp_path, "ag-zrec", _returning_driver)
    rec = read_exit_record(tmp_path / "ag-zrec")
    # Assert: the ExitRecord names the outage shape precisely.
    assert rec is not None and rec["reason"] == EXIT_HARNESS_RETURNED


def test_conversation_return_final_beat_is_stopping_not_green(tmp_path: Path) -> None:
    # Arrange
    from scitex_agent_container._runners._session_state import (
        STATE_STOPPING,
        read_heartbeat,
    )

    # Act
    _run_bounded(tmp_path, "ag-zbeat", _returning_driver)
    hb = read_heartbeat(tmp_path / "ag-zbeat")
    # Assert: no more green heartbeats over a consumer-less daemon.
    assert hb is not None and hb["state"] == STATE_STOPPING


# ---------------------------------------------------------------------------
# crashed: the driver dies with an exception
# ---------------------------------------------------------------------------


def test_daemon_exits_when_conversation_dies(tmp_path: Path) -> None:
    # Arrange
    # Act: pre-fix this too parks forever (the exception is swallowed by
    # the unobserved task); post-fix the daemon exits promptly.
    rc = _run_bounded(tmp_path, "ag-crash", _dying_driver)
    # Assert
    assert rc != 0


def test_conversation_death_writes_crashed_exit_record(tmp_path: Path) -> None:
    # Arrange
    # Act
    _run_bounded(tmp_path, "ag-crec", _dying_driver)
    rec = read_exit_record(tmp_path / "ag-crec")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_CRASHED
