"""Three-state health verdict for the ``sac listen`` flock holder.

Why three states (the false-green this closes)
==============================================
The standby loop used to ask ``_probe_health() -> bool``. A two-state
predicate cannot distinguish the two things an operator MUST be able to
tell apart:

* "the holder ANSWERED ``/v1/health``"  — an OBSERVATION, and
* "I asked and got NOTHING back"        — the ABSENCE of an observation.

Collapsed into one ``False``, the second becomes indistinguishable from
a merely-slow probe, and the loop's ``consecutive_unhealthy = 0`` reset
then let a single lucky reply erase the record of a failed check. The
holder was declared a ``healthy holder`` on the strength of nothing at
all, and ``sac listen`` stood by behind it forever while the fleet was
cut off from the host (operator incident 2026-07-14, PID 738982 on
127.0.0.1:7878).

Absence of evidence is NOT evidence of health. So the verdict is a
THREE-state enum, and the caller must branch on the state it actually
observed:

* :attr:`HolderHealth.SERVING` — the holder answered ``/v1/health``.
* :attr:`HolderHealth.NOT_SERVING` — the holder answered, with a 5xx.
  It is bound and speaking HTTP but its health route is erroring. An
  answer, but NOT health.
* :attr:`HolderHealth.UNREACHABLE` — we asked and got nothing back
  (connection refused / timed out / DNS). This is the "I have no
  observation" state. It is NOT healthy, and it must never be logged
  as one.

Why 4xx still counts as SERVING
===============================
A 401/403 PROVES the daemon is up: it is bound, speaking HTTP, and
auth-gating (:class:`~_listen.auth.BearerAuthMiddleware`). Card
``sac-listen-restart-healthcheck-bearer`` (PR #463) was written because
gating liveness on ``status == 200`` re-classified a live, 401-answering
daemon as "down" — a false-RED that SIGKILLed a HEALTHY process. That
lesson is load-bearing and is preserved here: a false-RED (destroying a
working daemon) is strictly worse than a false-green, so every response
below 500 — including a 404 from a daemon whose route table differs —
is SERVING. Only a *server error* (5xx) or *no answer at all* counts
against the holder.

Pure + dependency-light: stdlib ``urllib`` via ``_restart``'s
``_default_http_get``, which already returns ``-1`` for a transport
failure and the real status for any HTTP response.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ._restart import HEALTH_PATH
from ._restart import _default_http_get as _default_http_get_impl

__all__ = [
    "HEALTH_PATH",
    "HolderHealth",
    "HolderProbe",
    "classify_status",
    "probe_holder_health",
]

# The status at/above which an HTTP answer stops counting as "serving".
# Below it (2xx/3xx/4xx) the daemon is bound and answering — see the
# module docstring on why 401/403/404 must stay SERVING.
_SERVER_ERROR_FLOOR = 500


class HolderHealth(Enum):
    """What the flock holder ACTUALLY did when asked ``/v1/health``.

    Three states because two cannot express "I asked and got nothing".
    Only :attr:`SERVING` is health; the other two are NOT, and neither
    may ever be rendered to the operator as a "healthy holder".
    """

    SERVING = "serving"
    NOT_SERVING = "not-serving"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class HolderProbe:
    """One health observation: the verdict PLUS the evidence for it.

    ``status`` is the raw HTTP status the holder answered with, or
    ``-1`` when the probe got no answer at all. It is carried so every
    log line can state what was OBSERVED rather than assert a
    conclusion — the whole point of this module.
    """

    health: HolderHealth
    status: int

    @property
    def serving(self) -> bool:
        """True ONLY when the holder answered ``/v1/health``."""
        return self.health is HolderHealth.SERVING

    def describe(self) -> str:
        """Human-readable evidence for this verdict (for the log line)."""
        if self.health is HolderHealth.SERVING:
            return f"{HEALTH_PATH} answered HTTP {self.status}"
        if self.health is HolderHealth.NOT_SERVING:
            return (
                f"{HEALTH_PATH} answered HTTP {self.status} "
                f"— a server error, NOT health"
            )
        return (
            f"{HEALTH_PATH} did not answer AT ALL "
            f"(connection refused / timed out) — no evidence of health"
        )


def classify_status(status: int) -> HolderHealth:
    """Map an ``_default_http_get`` status onto the three-state verdict.

    ``status < 0`` is ``_default_http_get``'s transport-failure sentinel
    (refused / timeout / DNS): we asked and got nothing ⇒ UNREACHABLE.
    ``>= 500`` ⇒ answered-but-erroring ⇒ NOT_SERVING. Everything else
    ⇒ SERVING (see the module docstring on 401/403/404).
    """
    if status < 0:
        return HolderHealth.UNREACHABLE
    if status >= _SERVER_ERROR_FLOOR:
        return HolderHealth.NOT_SERVING
    return HolderHealth.SERVING


def probe_holder_health(
    host: str,
    port: int,
    *,
    timeout: float,
    http_get: Callable[[str, float], int] | None = None,
) -> HolderProbe:
    """Ask ``http://<host>:<port>/v1/health`` and return what came back.

    BOUNDED by ``timeout`` so a hung holder cannot stall the probe —
    a probe that never returns is itself an unbounded wait, and this
    module exists to make waiting bounded and observable.

    ``http_get`` is injectable so callers/tests can drive the classifier
    without a socket; the default is the real stdlib-``urllib`` GET.
    """
    url = f"http://{host}:{port}{HEALTH_PATH}"
    get = http_get or _default_http_get_impl
    status = int(get(url, timeout))
    return HolderProbe(health=classify_status(status), status=status)
