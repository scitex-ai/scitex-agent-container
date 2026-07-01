"""Fail-loud watchdog for the ``sac listen`` bind (cards
``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``).

The incident this guards: the listen daemon started, the lifespan
launched its background loops, but uvicorn NEVER bound the port — and
NOTHING was logged. An up-but-not-serving daemon is the worst failure
mode because it is invisible: the whole fleet lost agent-to-agent comms
with no error anywhere.

Operator directive ("when failure occurs, fail loud; explicit feedback
loop with errors and hints"): the silent up-but-not-serving state must
become IMPOSSIBLE. This watchdog runs as a lifespan task that, ``delay_s``
seconds after startup, probes ``http://127.0.0.1:<port>/v1/health``. If
the probe fails (connection refused / timeout — i.e. the server is not
actually serving), it logs a LOUD ``ERROR`` naming the likely cause and
an actionable hint, then keeps re-probing on an interval so the alarm
persists for as long as the daemon is wedged. Once the probe succeeds it
logs a single info line and exits (the bind is healthy; the watchdog has
done its job).

Pure-stdlib (``urllib``) probe so it adds no dependency and is itself
non-blocking: the blocking ``urlopen`` is dispatched off the event loop
via :func:`_off_loop.run_blocking`.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How long after startup to first check the bind. Generous enough that a
# healthy server is always serving by then, short enough that the operator
# learns about a wedged daemon within seconds rather than via a silent
# fleet outage.
DEFAULT_WATCHDOG_DELAY_S = 15.0

# Re-probe cadence while the bind is still down, so the LOUD alarm keeps
# firing (a single line at T+15s is easy to miss in a long log).
DEFAULT_WATCHDOG_REPROBE_S = 30.0


def _probe_health(url: str, timeout_s: float) -> bool:
    """True iff an HTTP GET to ``url`` connects and returns a response.

    Any HTTP status counts as "serving" — even a 401 from the bearer
    middleware means uvicorn bound the port and is handling requests,
    which is exactly what we are checking. Only a connection-level
    failure (refused / timeout / DNS) means "not serving".
    """
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(url, timeout=timeout_s)  # noqa: S310 (localhost only)
        return True
    except urllib.error.HTTPError:
        # Got an HTTP response (e.g. 401) → the server IS serving.
        return True
    except Exception:  # stx-allow: fallback (connection-level failure = not serving; that is the signal we want)
        return False


async def bind_watchdog_loop(
    *,
    port: int,
    host: str = "127.0.0.1",
    delay_s: float = DEFAULT_WATCHDOG_DELAY_S,
    reprobe_s: float = DEFAULT_WATCHDOG_REPROBE_S,
    probe=None,
) -> None:
    """Lifespan task: fail loud if the listen bind is not serving.

    Waits ``delay_s``, then probes ``http://{host}:{port}/v1/health``.
    On success: logs one info line and returns. On failure: logs a LOUD
    ERROR (cause + hint) and keeps re-probing every ``reprobe_s`` until
    the bind comes up or the task is cancelled (lifespan teardown).

    ``probe`` is a test seam ``probe(url, timeout_s) -> bool``; production
    uses :func:`_probe_health` dispatched off the event loop.
    """
    from ._off_loop import run_blocking

    url = f"http://{host}:{port}/v1/health"
    probe_fn = probe if probe is not None else _probe_health

    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:
        raise

    alarmed = False
    while True:
        try:
            ok = await run_blocking(probe_fn, url, 3.0, timeout_s=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # stx-allow: fallback (probe dispatch error = treat as not-serving; keep alarming)
            ok = False

        if ok:
            if alarmed:
                logger.info(
                    "bind_watchdog: %s is now serving — listen bind recovered.",
                    url,
                )
            else:
                logger.info("bind_watchdog: listen bind healthy (%s).", url)
            return

        alarmed = True
        logger.error(
            "bind_watchdog: ALARM — `sac listen` did NOT bind/serve %s within "
            "%.0fs of startup. The daemon is UP but NOT SERVING — the entire "
            "fleet's agent-to-agent comms are DOWN with no other error. "
            "LIKELY CAUSE: a startup background loop made a blocking call "
            "(gh/tmux/network subprocess or a socket call) on the asyncio "
            "event loop, starving uvicorn's bind. HINT: restart with "
            "SAC_PERIODIC_DRIVE_DISABLED=1 SAC_GITHUB_CI_POLLER_DISABLED=1 "
            "SAC_TUI_HEARTBEAT_DISABLED=1 to confirm, then check the loop "
            "startup paths (must use _off_loop.run_blocking). Re-probing in "
            "%.0fs.",
            url,
            delay_s,
            reprobe_s,
        )
        try:
            await asyncio.sleep(reprobe_s)
        except asyncio.CancelledError:
            raise


__all__ = [
    "DEFAULT_WATCHDOG_DELAY_S",
    "DEFAULT_WATCHDOG_REPROBE_S",
    "bind_watchdog_loop",
]
