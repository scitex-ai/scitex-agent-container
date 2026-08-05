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
    "probe_listen_authed",
    "probe_listen_health",
    "transport_failure_message",
]

# The one path ``BearerAuthMiddleware`` exempts (``PUBLIC_PATHS``).
HEALTH_PATH = "/v1/health"

# A SECOND, AUTHENTICATED route to compare against — the whole reason this
# module can now say something instead of only admitting ignorance.
#
# scitex-dev asked for exactly this, twice: "the observation is cheap (it
# already probes /v1/health — probing one authenticated route alongside it
# would have shown this immediately)". They were right, and the previous
# version merely TOLD the reader to go run this call.
#
# Why this route specifically:
#   * AUTHENTICATED — not in ``PUBLIC_PATHS``, so it exercises the bearer
#     path that ``/v1/health`` cannot speak to.
#   * On a DIFFERENT prefix from ``/agents`` — which is what the observed
#     failures track (``POST /agents`` and ``POST /agents/<n>/send`` both
#     hung while ``POST /v1/host_exec`` answered in ~2.4s, same daemon,
#     same minutes).
#   * A read-only GET. Deliberately NOT ``POST /v1/host_exec``, which
#     EXECUTES a command on the host and writes an audit line — unacceptable
#     in a probe that fires on every transport failure.
AUTHED_PATH = "/v1/host_exec/inflight"

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
    authenticated: bool = False

    @property
    def authorized(self) -> bool:
        """Did an AUTHENTICATED request get past the middleware and be served?

        ``serving`` is deliberately weaker: a 401 is an HTTP response, so it
        proves the daemon is up. But a 401 is produced by
        ``BearerAuthMiddleware`` BEFORE the handler runs, so on an
        authenticated probe it proves nothing about whether authenticated
        WORK is being served — the exact question this probe exists to
        answer. Conflating the two would make this probe lie in the one
        situation it was added for.
        """
        return self.serving and self.status not in (401, 403)

    def evidence(self) -> str:
        """One line of what we measured — quotable in an error message.

        THREE decimals, not two. Measured against the live daemon, the
        authenticated probe answers in ~0.005s, which two decimals render as
        "in 0.00s" — a timing that reads like the measurement never ran. In a
        message whose entire purpose is being trustworthy about what it
        observed, a real reading must not look like an uninitialised one.
        """
        kind = "an authenticated" if self.authenticated else "an unauthenticated"
        if self.serving:
            return (
                f"{kind} GET {self.url} answered HTTP "
                f"{self.status} in {self.elapsed_s:.3f}s"
            )
        return (
            f"{kind} GET {self.url} got no HTTP response "
            f"({self.error}) after {self.elapsed_s:.3f}s"
        )


def _probe_get(
    url: str,
    *,
    headers: dict,
    timeout_s: float,
    opener: Optional[Callable],
    authenticated: bool,
) -> HealthProbe:
    """One GET, every failure mode recorded, never raises.

    Shared by both probes so their error handling cannot drift apart — the
    four branches below are exactly the distinctions the message depends on,
    and having two copies of them is how one copy quietly stops matching.
    """
    opener_fn = opener if opener is not None else urlrequest.urlopen
    req = urlrequest.Request(url, method="GET", headers=headers)
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
            authenticated=authenticated,
        )
    except urlerror.HTTPError as exc:
        # A RESPONSE. 401/403/404 all prove the daemon is up and SERVING —
        # the original reporter's own evidence was a 401. HTTPError subclasses
        # URLError, so this MUST be caught first or a serving daemon would
        # be misfiled as unreachable (the exact bug, one level down).
        # NOTE: for an AUTHENTICATED probe, ``serving`` is not the question —
        # see ``HealthProbe.authorized``.
        return HealthProbe(
            serving=True,
            status=int(getattr(exc, "code", 0)) or None,
            elapsed_s=time.monotonic() - started,
            error=None,
            url=url,
            authenticated=authenticated,
        )
    except (urlerror.URLError, OSError, ValueError) as exc:
        # No HTTP exchange at all: connection refused / DNS / timeout.
        return HealthProbe(
            serving=False,
            status=None,
            elapsed_s=time.monotonic() - started,
            error=str(getattr(exc, "reason", None) or exc),
            url=url,
            authenticated=authenticated,
        )
    except Exception as exc:  # stx-allow: fallback (reason: this probe runs INSIDE another failure path — it exists to IMPROVE an error message and must never replace it with a crash. Any unexpected failure degrades to an honest "could not measure".)  # noqa: BLE001
        logger.warning("listen probe raised unexpectedly: %s", exc)
        return HealthProbe(
            serving=False,
            status=None,
            elapsed_s=time.monotonic() - started,
            error=f"probe failed: {exc}",
            url=url,
            authenticated=authenticated,
        )


def probe_listen_health(
    base: str,
    *,
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    opener: Optional[Callable] = None,
) -> HealthProbe:
    """GET ``{base}/v1/health`` with NO Authorization header. Never raises.

    Deliberately unauthenticated: this answers "is the daemon serving HTTP at
    all", and nothing more. What it CANNOT tell you is whether authenticated
    work is being served — that is what :func:`probe_listen_authed` is for,
    and assuming this one covered it is precisely the error that put a wrong
    diagnosis in this module's output for a month.
    """
    return _probe_get(
        f"{base.rstrip('/')}{HEALTH_PATH}",
        headers={"Accept": "application/json"},
        timeout_s=timeout_s,
        opener=opener,
        authenticated=False,
    )


def probe_listen_authed(
    base: str,
    token: str | None,
    *,
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    opener: Optional[Callable] = None,
) -> HealthProbe | None:
    """GET an AUTHENTICATED, read-only route with the bearer. Never raises.

    Returns ``None`` when there is no token — with nothing to authenticate
    with, a request here would 401 and that 401 would say nothing about the
    daemon. An honest "did not measure" beats a measurement that cannot mean
    what the reader will take it to mean.

    Read :data:`AUTHED_PATH` for why this route and not ``POST /v1/host_exec``.
    """
    if not token:
        return None
    return _probe_get(
        f"{base.rstrip('/')}{AUTHED_PATH}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout_s=timeout_s,
        opener=opener,
        authenticated=True,
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
    authed_probe: HealthProbe | None = None,
) -> str:
    """The honest message for a brokered POST that got no HTTP response.

    The daemon answered nothing at all → it is genuinely unreachable. Say
    that, and do not speculate about *why*.

    Otherwise the daemon is UP, and what we can say next depends on whether
    a SECOND, AUTHENTICATED route was measured (``authed_probe``):

      * it answered → the daemon is serving authenticated work, so the fault
        is specific to THIS route. A restart is not indicated, and we say so.
      * it also hung → the fault is shared. A restart is worth its cost.
      * it was rejected (401/403) or not attempted → we measured nothing
        about authenticated work and must not pretend otherwise.

    ``verb`` is the operation ("restart" / "spawn"), ``route`` the path
    that timed out (e.g. ``POST /agents/x/restart``).
    """
    if probe.serving:
        head = (
            f"{verb} of {name!r} failed: {route} to {base!r} got no response "
            f"within {timeout_s:.0f}s ({exc}).\n"
            f"OBSERVED: {probe.evidence()} — so the listen daemon is UP and "
            f"serving. The daemon is NOT down and this is NOT a 'broker "
            f"unreachable' failure: this ONE route did not answer.\n"
        )
        if authed_probe is None:
            return head + (
                f"NOT ESTABLISHED — why. {HEALTH_PATH} is UNAUTHENTICATED, so "
                f"it does not tell you whether authentication, this handler, "
                f"or the work behind it is at fault, and no authenticated "
                f"route was measured (no bearer token available).\n"
                f"NEXT, to find out rather than guess: call an authenticated "
                f"route (e.g. GET {AUTHED_PATH}) and compare. If it answers, "
                f"the fault is specific to {route} and restarting the daemon "
                f"will not fix it. If it also hangs, the fault is shared and "
                f"a restart is worth trying — `sac listen restart` on the "
                f"host, which interrupts EVERY agent mid-operation, so "
                f"establish that it is shared first."
            )
        if not authed_probe.serving:
            return head + (
                f"ALSO OBSERVED: {authed_probe.evidence()}. So an "
                f"authenticated route on a DIFFERENT prefix hung too, while "
                f"the public path answered — the fault is SHARED across "
                f"authenticated work, not specific to {route}.\n"
                f"On this evidence `sac listen restart` on the host is worth "
                f"its cost. It interrupts EVERY agent mid-operation, so say "
                f"so when you run it."
            )
        if not authed_probe.authorized:
            return head + (
                f"ALSO OBSERVED: {authed_probe.evidence()} — but that is an "
                f"AUTH REJECTION, produced by the middleware BEFORE any "
                f"handler runs, so it says nothing about whether "
                f"authenticated work is being served.\n"
                f"NOT ESTABLISHED — why {route} hung. Fix the bearer "
                f"(SAC_LISTEN_BEARER / the host token file) and retry to get "
                f"a real second reading. Do NOT restart the daemon on this: "
                f"nothing here indicates a daemon-wide fault."
            )
        return head + (
            f"ALSO OBSERVED: {authed_probe.evidence()}. So the daemon is "
            f"serving AUTHENTICATED work fine on a different prefix, in the "
            f"same seconds that {route} did not answer.\n"
            f"THEREFORE the fault is specific to {route}, not daemon-wide. "
            f"Do NOT run `sac listen restart` for this — it interrupts every "
            f"agent on the box and would not address a per-route fault. "
            f"Retry, and if it recurs report {route} with these two timings; "
            f"the route has been seen to answer and then degrade, so a single "
            f"success afterwards does not mean it was fixed."
        )
    return (
        f"{verb} of {name!r} failed: cannot reach listen at {base!r} ({exc}).\n"
        f"MEASURED (not guessed): {probe.evidence()} — nothing is serving HTTP "
        f"at this URL, so the daemon is DOWN or the URL/port is wrong.\n"
        f"Fix: start it on the host with `sac listen restart` (an atomic "
        f"stop-clean-relaunch) and retry; escalate to the operator only if it "
        f"stays down."
    )
