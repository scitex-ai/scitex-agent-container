"""Run blocking calls OFF the asyncio event loop, with a hard timeout.

Root-cause guard for the silent ``sac listen`` bind hang (cards
``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``):

The listen lifespan launches its background loops via ``create_task``
*before* uvicorn binds the port. Each loop's coroutine body runs its
synchronous preflight (``gh auth status``, ``tmux`` probes, a registry
walk) BEFORE its first ``await``. A synchronous ``subprocess.run`` /
socket call on those code paths runs ON the event loop thread — so a
hung ``gh``/``tmux``/network call starves uvicorn's own bind (which is
also a loop task), and the daemon comes up but never serves, with NO
error logged. A full silent fleet-comms outage.

This helper makes that class of bug impossible: every blocking call a
loop makes goes through :func:`run_blocking`, which (1) dispatches the
call to a thread so it never occupies the loop thread, and (2) wraps it
in ``asyncio.wait_for`` so a wedged call can never hang forever — it
raises :class:`asyncio.TimeoutError` after ``timeout_s`` and the caller
logs + degrades instead of blocking the fleet.

DEDICATED THREADS, NOT THE SHARED DEFAULT EXECUTOR
--------------------------------------------------
The dispatch deliberately spawns a fresh daemon thread per call instead
of ``loop.run_in_executor(None, ...)``. That is not a style choice — the
executor form reintroduced, by a longer route, the exact fleet-comms
hang this module exists to prevent:

A ``concurrent.futures`` future whose function is ALREADY RUNNING cannot
be cancelled. So when ``wait_for`` times out, the worker thread is NOT
stopped — it keeps running the wedged call forever, permanently holding
its slot in the event loop's *shared* default ``ThreadPoolExecutor``.
That pool is only ``min(32, os.cpu_count() + 4)`` threads — just 6-8 on
a 2-4 core CI runner. Six background loops (tui/sdk heartbeat, liveness
tick, bind watchdog, gh CI poll, periodic drive) each abandon a thread
every time a tick overruns, so the pool drains steadily. Once it is
empty, EVERY other user of the default executor starves — and the rest
of the codebase reaches that same pool through *unbounded*
``asyncio.to_thread`` calls (the ``/agents`` spawn handler's brokered
launch, ``_host_exec``, ``_agent_exec_send``, ``_forward``). Those have
no ``wait_for`` to save them: they queue behind the wedged threads and
hang FOREVER. Measured: after ``max_workers`` timed-out ``run_blocking``
calls, a trivial ``to_thread(lambda: "ok")`` never runs at all. This is
version-independent (reproduced identically on 3.11 and 3.12); it simply
bites soonest where the pool is smallest, which is why it surfaced as an
intermittent "only on the CI runner" hang of the pytest suite.

A dedicated daemon thread per call restores the intended semantics: an
abandoned call leaks exactly ONE thread (unavoidable — Python cannot
kill a thread blocked in a syscall) and can never starve anything else.
The threads are daemons, so a wedged probe also cannot hold interpreter
exit open.

Fail-loud (operator: "when failure occurs, fail loud"): a timeout is
returned to the caller as an explicit exception (or, via
:func:`run_blocking_or`, a sentinel default + a logged ERROR naming the
op), never silently swallowed into an indistinguishable success. The
count of still-wedged abandoned calls is tracked and logged loudly once
it crosses :data:`_ABANDONED_WARN_THRESHOLD`, so a genuinely stuck host
is visible instead of quietly leaking threads.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default ceiling for any single off-loop blocking call made from a
# listen background loop. A healthy ``gh``/``tmux`` probe returns in well
# under a second; this generous floor only ever fires on a genuinely
# wedged call (dead network, hung child) — exactly the case that used to
# take the whole fleet's comms down.
DEFAULT_BLOCKING_TIMEOUT_S = 5.0

# Abandoned calls still running past their deadline. A couple is normal on
# a loaded host (a slow probe that eventually returns); a persistently high
# count means a genuinely stuck subprocess/socket and is worth an ERROR.
_ABANDONED_WARN_THRESHOLD = 8

_abandoned_lock = threading.Lock()
_abandoned_count = 0


def abandoned_call_count() -> int:
    """Off-loop calls that timed out and whose thread is STILL running.

    Diagnostic gauge for the fail-loud path (and the regression test that
    pins "an abandoned call must not starve unrelated off-loop work").
    """
    with _abandoned_lock:
        return _abandoned_count


def _mark_abandoned(op: str) -> None:
    global _abandoned_count
    with _abandoned_lock:
        _abandoned_count += 1
        current = _abandoned_count
    if current >= _ABANDONED_WARN_THRESHOLD:
        logger.error(
            "off_loop: %d abandoned blocking calls are STILL wedged (latest: "
            "%s). Each leaks one daemon thread. A stuck subprocess "
            "(gh/tmux/ssh) or an unresponsive host is the usual cause.",
            current,
            op,
        )


def _clear_abandoned() -> None:
    global _abandoned_count
    with _abandoned_lock:
        _abandoned_count -= 1


async def run_blocking(
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float = DEFAULT_BLOCKING_TIMEOUT_S,
    **kwargs: Any,
) -> T:
    """Run a blocking ``fn(*args, **kwargs)`` off the loop, bounded.

    The call is dispatched to a DEDICATED daemon thread — never the event
    loop's shared default executor — so it neither occupies the loop
    thread nor competes for the pool that ``asyncio.to_thread`` callers
    depend on. It is bounded by ``timeout_s`` via :func:`asyncio.wait_for`
    and raises :class:`asyncio.TimeoutError` if the call does not finish
    in time. The underlying thread is left to finish/abandon — we do NOT
    join it, so a wedged child can never block the loop; because the
    thread is private to this call, abandoning it starves nothing (see the
    module docstring: doing this on the shared executor is what made a
    wedged probe hang the whole process). Any exception ``fn`` raises
    propagates unchanged.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()
    op = getattr(fn, "__name__", "call")
    # Guards the abandoned/finished handoff so a call that completes at the
    # same instant its deadline fires is counted exactly once.
    state_lock = threading.Lock()
    finished = False
    abandoned = False

    def _deliver(result: Any, exc: BaseException | None) -> None:
        # On the loop thread. If wait_for already timed out it cancelled the
        # future and moved on — there is no awaiter left to deliver to.
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _runner() -> None:
        nonlocal finished
        result: Any = None
        exc: BaseException | None = None
        try:
            result = fn(*args, **kwargs)
        except BaseException as caught:  # stx-allow: fallback (reason: relayed verbatim to the awaiter, never swallowed)
            exc = caught
        with state_lock:
            finished = True
            was_abandoned = abandoned
        if was_abandoned:
            # We outlived our deadline; nobody is waiting. Just stop counting.
            _clear_abandoned()
            return
        try:
            loop.call_soon_threadsafe(_deliver, result, exc)
        except RuntimeError:  # stx-allow: fallback (reason: loop already closed — the awaiter is long gone; dropping the result is correct)
            pass

    threading.Thread(
        target=_runner, name=f"sac-off-loop-{op}", daemon=True
    ).start()
    try:
        return await asyncio.wait_for(future, timeout=timeout_s)
    except asyncio.TimeoutError:
        with state_lock:
            newly_abandoned = not finished
            if newly_abandoned:
                abandoned = True
        if newly_abandoned:
            _mark_abandoned(op)
        raise


async def run_blocking_or(
    fn: Callable[..., T],
    *args: Any,
    default: T,
    op: str,
    timeout_s: float = DEFAULT_BLOCKING_TIMEOUT_S,
    **kwargs: Any,
) -> T:
    """Like :func:`run_blocking` but degrade to ``default`` on timeout/error.

    A timeout or an exception from ``fn`` is logged at ERROR (fail-loud:
    the message names ``op`` and, for a timeout, the ceiling that fired)
    and ``default`` is returned, so a single wedged probe degrades that
    tick rather than wedging the loop. ``op`` is a short human label for
    the operation (e.g. ``"gh auth status"``).
    """
    try:
        return await run_blocking(fn, *args, timeout_s=timeout_s, **kwargs)
    except asyncio.TimeoutError:
        logger.error(
            "off_loop: %s exceeded %.1fs and was abandoned (it was about to "
            "block the listen event loop / uvicorn bind). Degrading this "
            "call to its safe default. Hint: a hung subprocess "
            "(gh/tmux/network) or unresponsive host is the usual cause.",
            op,
            timeout_s,
        )
        return default
    except Exception as exc:  # stx-allow: fallback (off-loop probe failure degrades this call, never wedges the loop)
        logger.warning("off_loop: %s failed (%s); degrading to default", op, exc)
        return default


__all__ = [
    "DEFAULT_BLOCKING_TIMEOUT_S",
    "abandoned_call_count",
    "run_blocking",
    "run_blocking_or",
]
