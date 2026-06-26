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
call to a thread via ``run_in_executor`` so it never occupies the loop
thread, and (2) wraps it in ``asyncio.wait_for`` so a wedged call can
never hang forever — it raises :class:`asyncio.TimeoutError` after
``timeout_s`` and the caller logs + degrades instead of blocking the
fleet.

Fail-loud (operator: "when failure occurs, fail loud"): a timeout is
returned to the caller as an explicit exception (or, via
:func:`run_blocking_or`, a sentinel default + a logged ERROR naming the
op), never silently swallowed into an indistinguishable success.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default ceiling for any single off-loop blocking call made from a
# listen background loop. A healthy ``gh``/``tmux`` probe returns in well
# under a second; this generous floor only ever fires on a genuinely
# wedged call (dead network, hung child) — exactly the case that used to
# take the whole fleet's comms down.
DEFAULT_BLOCKING_TIMEOUT_S = 5.0


async def run_blocking(
    fn: Callable[..., T],
    *args: Any,
    timeout_s: float = DEFAULT_BLOCKING_TIMEOUT_S,
    **kwargs: Any,
) -> T:
    """Run a blocking ``fn(*args, **kwargs)`` off the loop, bounded.

    The call is dispatched to the default thread-pool executor so it
    never occupies the event-loop thread, and bounded by ``timeout_s``
    via :func:`asyncio.wait_for`. Raises :class:`asyncio.TimeoutError`
    if the call does not finish in time (the underlying thread is left
    to finish/abandon — we do NOT join it, so a wedged child can never
    block the loop). Any exception ``fn`` raises propagates unchanged.
    """
    loop = asyncio.get_running_loop()

    def _call() -> T:
        return fn(*args, **kwargs)

    return await asyncio.wait_for(
        loop.run_in_executor(None, _call), timeout=timeout_s
    )


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
    "run_blocking",
    "run_blocking_or",
]
