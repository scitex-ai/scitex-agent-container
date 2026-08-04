"""MEASURE the listen daemon before naming what is wrong with it.

Shared by :mod:`._restart_client` and :mod:`._spawn_client`: when a
brokered POST to the host ``sac listen`` gets no HTTP response, this
module answers the ONLY question the caller can actually settle — *is
the daemon down, or is it up and serving while the AUTHENTICATED route
is wedged?* — by probing the cheap unauthenticated path.

THE BUG THIS CLOSES (card ``sac-restart-prints-success-after-start-failed``,
2026-07-14). An agent trying to recover a peer got::

    cannot reach listen at 'http://127.0.0.1:7878' (timed out) — the host
    listen broker is unreachable; it may be flapping. Restart it on the
    host with `sac listen restart`.

Every clause after the parenthesis was invented. The reporter measured::

    GET  /health           (unauthenticated) -> HTTP 401 in 0.18s, twice
    GET  /agents           (with bearer)     -> no response in 10s
    POST /agents/<n>/restart (with bearer)   -> no response in 25s

The daemon was UP and answering in under a fifth of a second. It was not
"unreachable" and there was not one shred of evidence for "flapping" — a
word that asserts a process is crash-looping, which nobody had looked at.
A timeout on ONE route licenses a claim about THAT ROUTE, and nothing more.

WHAT THIS MODULE MAY AND MAY NOT CONCLUDE
-----------------------------------------
It probes exactly ONE route — ``/v1/health``, which is UNAUTHENTICATED —
so it can settle "is the daemon serving HTTP at all" and nothing else.

It used to go further. A previous version explained the split structurally:
``BearerAuthMiddleware.PUBLIC_PATHS`` exempts ``/v1/health``, that route and
the middleware's 401 are ``async`` and answer on the event loop, while
authenticated routes dispatch through a shared worker pool — therefore an
exhausted pool wedges the authed routes and spares the public one.

THAT INFERENCE IS REFUTED (scitex-dev, 2026-08-04). ``POST /v1/host_exec``
is AUTHENTICATED and answered in ~2.4s with a 127KB payload while
``POST /agents`` hung, measured seconds apart on the same daemon. A pool
shared by both cannot wedge one and spare the other, so pool exhaustion
cannot be the mechanism — whatever the routing structure is.

The structural facts above may still be true; what does not follow is the
CAUSE. One observation on an unauthenticated route cannot distinguish
authentication, this handler, or the work behind it. So the message names
no cause and instead tells the reader how to get one: call a second
AUTHENTICATED route and compare.

This is the SECOND wrong story this message has carried. The first
("unreachable; it may be flapping") was corrected in 2026-07 by replacing
it with the pool story — a better-sounding cause rather than no cause. Both
prescribed ``sac listen restart``, which interrupts every agent on the box.
The lesson is not "find the right explanation"; it is that this module is
not positioned to have one.

CONTRACT: any HTTP response at all — 200, 401, 403, 404 — proves the daemon
is UP and serving HTTP. Only the ABSENCE of an HTTP exchange (connection
refused / DNS / timeout) is evidence of "unreachable". We report what we
measured and never more than that: the word "flapping" appears nowhere in
this module's output, because nothing here can observe a crash loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

__all__ = [
    "HealthProbe",
    "probe_listen_health",
    "transport_failure_message",
]

# The one path ``BearerAuthMiddleware`` exempts (``PUBLIC_PATHS``), served
# by an ``async`` handler that never touches the shared worker pool.
HEALTH_PATH = "/v1/health"

# The probe must be CHEAP: it runs inside the failure path of a request
# that has ALREADY timed out, so it must not add another long wait. A
# healthy listen answers /v1/health in ~0.2s; 5s is 25x that.
_DEFAULT_PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class HealthProbe:
    """What an UNAUTHENTICATED ``GET /v1/health`` actually did.

    ``serving`` is the load-bearing field and it means exactly one thing:
    an HTTP response arrived. NOT "the daemon is healthy" — a 401 is a
    perfectly good proof of serving. Callers must not upgrade it into a
    broader claim.
    """

    serving: bool
    status: int | None
    elapsed_s: float
    error: str | None
    url: str

    def evidence(self) -> str:
        """One line of what we measured — quotable in an error message."""
        if self.serving:
            return (
                f"an unauthenticated GET {self.url} answered HTTP "
                f"{self.status} in {self.elapsed_s:.2f}s"
            )
        return (
            f"an unauthenticated GET {self.url} also got no HTTP response "
            f"({self.error}) after {self.elapsed_s:.2f}s"
        )


def probe_listen_health(
    base: str,
    *,
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    opener: Optional[Callable] = None,
) -> HealthProbe:
    """GET ``{base}/v1/health`` with NO Authorization header. Never raises.

    Deliberately unauthenticated: sending the bearer would route the probe
    through the very code path we are trying to rule in or out, and would
    hang with it. The point is to exercise the path that CANNOT be wedged
    by a starved worker pool.
    """
    url = f"{base.rstrip('/')}{HEALTH_PATH}"
    opener_fn = opener if opener is not None else urlrequest.urlopen
    req = urlrequest.Request(url, method="GET", headers={"Accept": "application/json"})
    started = time.monotonic()
    try:
        with opener_fn(req, timeout=timeout_s) as resp:
            resp.read()
            status = int(getattr(resp, "status", 200))
        return HealthProbe(
            serving=True,
            status=status,
            elapsed_s=time.monotonic() - started,
            error=None,
            url=url,
        )
    except urlerror.HTTPError as exc:
        # A RESPONSE. 401/403/404 all prove the daemon is up and serving —
        # the reporter's own evidence was a 401. HTTPError subclasses
        # URLError, so this MUST be caught first or a serving daemon would
        # be misfiled as unreachable (the exact bug, one level down).
        return HealthProbe(
            serving=True,
            status=int(getattr(exc, "code", 0)) or None,
            elapsed_s=time.monotonic() - started,
            error=None,
            url=url,
        )
    except (urlerror.URLError, OSError, ValueError) as exc:
        # No HTTP exchange at all: connection refused / DNS / timeout.
        return HealthProbe(
            serving=False,
            status=None,
            elapsed_s=time.monotonic() - started,
            error=str(getattr(exc, "reason", None) or exc),
            url=url,
        )
    except Exception as exc:  # stx-allow: fallback (reason: this probe runs INSIDE another failure path — it exists to IMPROVE an error message and must never replace it with a crash. Any unexpected failure degrades to an honest "could not measure".)  # noqa: BLE001
        logger.warning("listen health probe raised unexpectedly: %s", exc)
        return HealthProbe(
            serving=False,
            status=None,
            elapsed_s=time.monotonic() - started,
            error=f"probe failed: {exc}",
            url=url,
        )


def transport_failure_message(
    *,
    verb: str,
    name: str,
    base: str,
    route: str,
    exc: BaseException,
    timeout_s: float,
    probe: HealthProbe,
) -> str:
    """The honest message for a brokered POST that got no HTTP response.

    Two cases, and they are the two the caller can actually TELL APART:

      * the daemon answered the cheap path → it is UP; the AUTHENTICATED
        route is what failed. Say that, and only that.
      * the daemon answered nothing at all → it is genuinely unreachable.
        Say that, and still do not speculate about *why*.

    ``verb`` is the operation ("restart" / "spawn"), ``route`` the path
    that timed out (e.g. ``POST /agents/x/restart``).
    """
    if probe.serving:
        return (
            f"{verb} of {name!r} failed: {route} to {base!r} got no response "
            f"within {timeout_s:.0f}s ({exc}).\n"
            f"OBSERVED: {probe.evidence()} — so the listen daemon is UP and "
            f"serving. The daemon is NOT down and this is NOT a 'broker "
            f"unreachable' failure: this ONE route did not answer.\n"
            f"NOT ESTABLISHED — why. {HEALTH_PATH} is UNAUTHENTICATED, so it "
            f"does not tell you whether authentication, this handler, or the "
            f"work behind it is at fault. Two observations, one of them on a "
            f"route with different properties, cannot single out a cause.\n"
            f"NEXT, to find out rather than guess: call a DIFFERENT "
            f"authenticated route (e.g. POST /v1/host_exec with a trivial "
            f"argv) and compare. If it answers, the fault is specific to "
            f"{route} and restarting the daemon will not fix it. If it also "
            f"hangs, the fault is shared and a restart is worth trying — "
            f"`sac listen restart` on the host, which interrupts EVERY agent "
            f"mid-operation, so establish that it is shared first."
        )
    return (
        f"{verb} of {name!r} failed: cannot reach listen at {base!r} ({exc}).\n"
        f"MEASURED (not guessed): {probe.evidence()} — nothing is serving HTTP "
        f"at this URL, so the daemon is DOWN or the URL/port is wrong.\n"
        f"Fix: start it on the host with `sac listen restart` (an atomic "
        f"stop-clean-relaunch) and retry; escalate to the operator only if it "
        f"stays down."
    )
