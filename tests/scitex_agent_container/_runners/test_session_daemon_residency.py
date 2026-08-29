"""The residency AXIS at the daemon (v4 step 6) — declared, not implied.

``spec.residency`` reaches :func:`session_daemon.run_session_daemon` as
an explicit parameter. Under ``one-shot`` a normal conversation
completion is the PLAN — the daemon exits 0 with ExitRecord reason
``oneshot-complete`` instead of parking (and instead of branding the
return a ``harness-returned`` violation). Under ``resident`` the step-5
contract is byte-identical: park on ``stop.wait()``; a driver that
returns on its own is still a violation; a crash is still a crash.

Reuses the zombie-suite harness patterns
(``test_session_daemon_zombie_exit.py``): bounded ``asyncio.wait_for``
so a regression to parking fails as a TimeoutError, hand-rolled
coroutines only. No mocks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._incarnation import (
    EXIT_CRASHED,
    EXIT_HARNESS_RETURNED,
    EXIT_ONESHOT_COMPLETE,
    read_exit_record,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)

#: Generous ceiling for "the daemon must EXIT on its own" — a regression
#: back to parking turns into a visible TimeoutError, not a hang.
_EXIT_DEADLINE_S = 10.0


async def _returning_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """Answers one turn, then RETURNS without setting ``stop``.

    Under ``resident`` this is the zombie shape (a violation); under
    ``one-shot`` the very same completion is the declared plan.
    """
    inbox = kwargs["inbox"]
    env = await inbox.get()
    if isinstance(env, TurnEnvelope) and not env.response.done():
        env.response.set_result("ack")


async def _honoring_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """Mirrors the real driver's ``exit_after`` handshake (stop + return)."""
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


async def _dying_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """Dies outright — one-shot must NOT paper over a crash."""
    raise RuntimeError("harness fell over")


def _run_bounded(tmp_path: Path, name: str, driver: Any, *, residency: str) -> int:
    """Run a headless mission daemon under ``residency`` with a deadline."""

    async def _scenario() -> int:
        return await asyncio.wait_for(
            session_daemon.run_session_daemon(
                name,
                turn_driver=driver,
                residency=residency,
                state_root=tmp_path,
                tick_seconds=0.01,
                mission="boot",
            ),
            timeout=_EXIT_DEADLINE_S,
        )

    return asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# one-shot: a normal completion is the plan
# ---------------------------------------------------------------------------


def test_one_shot_daemon_exits_zero_on_clean_completion(tmp_path: Path) -> None:
    # Arrange: a driver that completes its work and returns.
    driver = _returning_driver
    # Act: pre-axis this same shape exited 1 (harness-returned).
    rc = _run_bounded(tmp_path, "ag-os-rc", driver, residency="one-shot")
    # Assert: the declared plan is a SUCCESS exit.
    assert rc == 0


def test_one_shot_completion_writes_oneshot_complete_exit_record(
    tmp_path: Path,
) -> None:
    # Arrange
    driver = _returning_driver
    # Act
    _run_bounded(tmp_path, "ag-os-rec", driver, residency="one-shot")
    rec = read_exit_record(tmp_path / "ag-os-rec")
    # Assert: the ExitRecord names the PLANNED end, not a violation.
    assert rec is not None and rec["reason"] == EXIT_ONESHOT_COMPLETE


def test_one_shot_mission_envelope_carries_exit_after(tmp_path: Path) -> None:
    # Arrange: a driver honouring the real exit_after handshake — the
    # daemon must END the run when the mission completes instead of
    # parking for more turns (pre-axis this scenario TIMES OUT).
    driver = _honoring_driver
    # Act
    rc = _run_bounded(tmp_path, "ag-os-exit", driver, residency="one-shot")
    # Assert
    assert rc == 0


def test_one_shot_does_not_paper_over_a_crash(tmp_path: Path) -> None:
    # Arrange: one-shot declares a planned END, never an excuse.
    driver = _dying_driver
    # Act
    _run_bounded(tmp_path, "ag-os-crash", driver, residency="one-shot")
    rec = read_exit_record(tmp_path / "ag-os-crash")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_CRASHED


def test_one_shot_crash_still_exits_nonzero(tmp_path: Path) -> None:
    # Arrange
    driver = _dying_driver
    # Act
    rc = _run_bounded(tmp_path, "ag-os-crc", driver, residency="one-shot")
    # Assert
    assert rc != 0


# ---------------------------------------------------------------------------
# resident: the step-5 contract is unchanged
# ---------------------------------------------------------------------------


def test_explicit_resident_keeps_harness_returned_on_stray_return(
    tmp_path: Path,
) -> None:
    # Arrange: the SAME driver that one-shot blesses is, under an
    # explicit resident declaration, still the zombie-shape violation.
    driver = _returning_driver
    # Act
    _run_bounded(tmp_path, "ag-res-rec", driver, residency="resident")
    rec = read_exit_record(tmp_path / "ag-res-rec")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_HARNESS_RETURNED


def test_explicit_resident_stray_return_exits_nonzero(tmp_path: Path) -> None:
    # Arrange
    driver = _returning_driver
    # Act
    rc = _run_bounded(tmp_path, "ag-res-rc", driver, residency="resident")
    # Assert
    assert rc != 0


# ---------------------------------------------------------------------------
# the parameter itself: explicit, closed, fail-loud
# ---------------------------------------------------------------------------


def test_daemon_refuses_an_unknown_residency(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> int:
        return await session_daemon.run_session_daemon(
            "ag-bad",
            turn_driver=_returning_driver,
            residency="half-shot",
            state_root=tmp_path,
        )

    # Act
    run = _scenario
    # Assert: refused loudly BEFORE any state is touched.
    with pytest.raises(ValueError, match="one-shot"):
        asyncio.run(run())


def test_run_threads_residency_through_to_the_daemon(tmp_path: Path) -> None:
    # Arrange: the harness-seam wrapper (claude_session.run) with its
    # test seam as the driver — the axis must SURVIVE the passthrough,
    # observable as the one-shot ExitRecord.
    from scitex_agent_container._runners.claude_session import run

    async def _scenario() -> int:
        return await asyncio.wait_for(
            run(
                "ag-thread",
                residency="one-shot",
                state_root=tmp_path,
                tick_seconds=0.01,
                mission="boot",
                run_conversation_fn=_returning_driver,
            ),
            timeout=_EXIT_DEADLINE_S,
        )

    # Act
    asyncio.run(_scenario())
    rec = read_exit_record(tmp_path / "ag-thread")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_ONESHOT_COMPLETE
