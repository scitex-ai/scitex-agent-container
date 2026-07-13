"""``_off_loop`` — bounded off-loop dispatch, and the starvation regression.

The regression these pin (CI hang, 2026-07-13, runs 29274230213 /
29273968878): ``run_blocking`` used to dispatch through
``loop.run_in_executor(None, ...)`` — the event loop's SHARED default
``ThreadPoolExecutor``. A ``concurrent.futures`` future that is already
running cannot be cancelled, so when ``wait_for`` timed out the worker
thread kept running the wedged call forever and permanently held its slot
in that shared pool. The pool is only ``min(32, os.cpu_count() + 4)``
threads (6-8 on a 2-4 core CI runner), and six listen background loops
abandon a thread every time a tick overruns. Once the pool was drained,
every OTHER user of the default executor starved — including the
*unbounded* ``asyncio.to_thread`` calls in the ``/agents`` spawn handler,
``_host_exec``, ``_agent_exec_send`` and ``_forward``, which have no
``wait_for`` to rescue them and so hung FOREVER. That is what killed the
pytest suite (a test would print its name and never finish) and, in
production, would wedge brokered spawns while the listen daemon silently
degraded every probe to its fallback.

NO MOCKS — a real ``threading.Event`` that is never set makes a real call
block in a real thread; the assertions are on real dispatch behaviour.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from scitex_agent_container._lifecycle._off_loop import (
    abandoned_call_count,
    run_blocking,
    run_blocking_or,
)


def _default_pool_size() -> int:
    """Size of the event loop's default ThreadPoolExecutor."""
    return min(32, (os.cpu_count() or 1) + 4)


async def _abandon_a_pools_worth(wedge) -> None:
    """Time out enough wedged calls to drain the default executor.

    Uses ``run_blocking_or`` (degrades, never raises) so the arrange phase
    of a test carries no assertion of its own.
    """
    for _ in range(_default_pool_size() + 4):
        await run_blocking_or(wedge, default=None, op="wedged probe", timeout_s=0.05)


@pytest.fixture
def wedge():
    """A callable that blocks until the test releases it. Always released."""
    released = threading.Event()

    def blocker() -> str:
        released.wait()
        return "unblocked"

    try:
        yield blocker
    finally:
        # Let every abandoned daemon thread exit instead of leaking into
        # the rest of the session.
        released.set()


# ---------------------------------------------------------------------------
# Baseline contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_blocking_returns_the_value():
    # Arrange
    def seven() -> int:
        return 7

    # Act
    got = await run_blocking(seven)
    # Assert
    assert got == 7


@pytest.mark.asyncio
async def test_run_blocking_propagates_the_exception_unchanged():
    # Arrange
    def boom() -> None:
        raise ValueError("kaboom")

    # Act
    call = run_blocking(boom)
    # Assert
    with pytest.raises(ValueError, match="kaboom"):
        await call


@pytest.mark.asyncio
async def test_run_blocking_times_out_on_a_wedged_call(wedge):
    # Arrange
    timeout_s = 0.2
    # Act
    call = run_blocking(wedge, timeout_s=timeout_s)
    # Assert
    with pytest.raises(asyncio.TimeoutError):
        await call


@pytest.mark.asyncio
async def test_run_blocking_or_degrades_to_default_on_timeout(wedge):
    # Arrange
    sentinel = "fallback"
    # Act
    got = await run_blocking_or(wedge, default=sentinel, op="probe", timeout_s=0.2)
    # Assert
    assert got == sentinel


@pytest.mark.asyncio
async def test_run_blocking_leaves_the_event_loop_free_to_run(wedge):
    """The loop must keep ticking while a blocking call is in flight."""
    # Arrange
    task = asyncio.create_task(
        run_blocking_or(wedge, default=None, op="probe", timeout_s=1.0)
    )
    ticks = 0
    # Act
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    # Assert
    assert ticks > 5


# ---------------------------------------------------------------------------
# THE REGRESSION — an abandoned call must starve nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandoned_calls_do_not_starve_asyncio_to_thread(wedge):
    """Abandon more calls than the default pool holds; ``to_thread`` must live.

    This is the exact shape of the CI hang: the ``/agents`` handler awaits an
    UNBOUNDED ``asyncio.to_thread`` for its brokered launch. Before the fix,
    the abandoned ``run_blocking`` threads owned every slot of the shared
    default executor and this ``to_thread`` never ran — the request never
    returned and the test hung forever.
    """
    # Arrange
    await _abandon_a_pools_worth(wedge)

    def healthy() -> str:
        return "ok"

    # Act
    try:
        got = await asyncio.wait_for(asyncio.to_thread(healthy), timeout=10.0)
    except asyncio.TimeoutError:
        got = "STARVED: to_thread never ran — queued behind abandoned threads"
    # Assert
    assert got == "ok"


@pytest.mark.asyncio
async def test_abandoned_calls_do_not_starve_later_run_blocking(wedge):
    """A healthy probe must still run after a pool's worth of wedged ones.

    Otherwise every listen background loop degrades to its fallback forever:
    heartbeats stop, the liveness tick goes blind, and nothing says why.
    """
    # Arrange
    await _abandon_a_pools_worth(wedge)

    def healthy() -> str:
        return "healthy"

    # Act
    got = await run_blocking_or(
        healthy, default="STARVED: healthy probe never ran", op="probe", timeout_s=10.0
    )
    # Assert
    assert got == "healthy"


@pytest.mark.asyncio
async def test_abandoned_call_count_reports_the_wedged_calls(wedge):
    """The fail-loud gauge counts calls whose thread is still stuck."""
    # Arrange
    before = abandoned_call_count()
    # Act
    for _ in range(3):
        await run_blocking_or(wedge, default=None, op="wedged probe", timeout_s=0.05)
    # Assert
    assert abandoned_call_count() == before + 3
