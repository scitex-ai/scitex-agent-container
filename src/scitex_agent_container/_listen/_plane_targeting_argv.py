"""Recognise a host_exec command that would restart the plane serving it.

THE INCIDENT THIS CLOSES. ``POST /v1/host_exec`` runs commands on the host, and
it is SERVED BY the ``sac listen`` daemon. A command that restarts that daemon
therefore kills the process group serving its own request. Measured 2026-08-09
while propagating a merged fix::

    systemctl --user restart sac-listen.service   (via host_exec)
    -> {"exit_code": -15, "stdout": "", ...}

-15 is SIGTERM. Empty stdout, no status, no confirmation. The restart HAD
succeeded — verified afterwards by an independent probe answering 200 on the
first attempt — but the command could not say so, because the component
reporting the result was the component being restarted.

WHY THE CALLER CANNOT RECOVER FROM THAT. These three are indistinguishable:

    (a) the restart succeeded and killed my reporter   <- what happened
    (b) the restart failed and something killed us both
    (c) the command was killed for an unrelated reason

An operator or agent reading (a) as (b) retries — restarting a healthy plane,
or concluding the control plane is broken when it is fine. That is the same
"succeeded but reported failed" family as the spawn route, and scitex-storage
reported the CLI form of it on 2026-07-28: "a restart that kills its own
response connection must report ACCEPTED, else callers retry and STACK
restarts."

THE ANSWER IS ALREADY IN THIS CODEBASE. ``_listen/_agent_restart.py`` solves the
identical shape for agent self-restart — a detached, deferred bounce so the
response flushes first, answered ``202`` with ``self_restart="scheduled"``. This
module is the DETECTION half for the host_exec surface, so that mechanism can be
reused rather than a third variant invented.

DELIBERATELY CONSERVATIVE. The constitution warns that pattern matching lies, so
this matches a small, explicit set of well-known spellings rather than trying to
be clever, and the asymmetry of its errors is chosen on purpose:

  * a FALSE POSITIVE means a command mentioning the listen service is run
    detached and answered 202 instead of inline — recoverable, loud, and the
    caller is told exactly what happened;
  * a FALSE NEGATIVE is merely the status quo (the -15 above).

So it is safe to be incomplete and unsafe to be aggressive. Nothing here refuses
work; the caller always gets a real answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["PlaneTargetVerdict", "targets_listen_plane"]

# The systemd unit that runs the daemon on this fleet.
_LISTEN_UNIT_TOKENS = ("sac-listen",)

# Service managers whose argv can stop/restart that unit.
_SERVICE_MANAGERS = ("systemctl", "service")

# Verbs that would end the CURRENT daemon process. `start` is deliberately
# absent: starting an already-running daemon is a no-op and kills nobody, and
# starting a DOWN one is exactly the recovery we must not make harder.
_DISRUPTIVE_VERBS = ("stop", "restart", "kill", "reload-or-restart", "try-restart")

# Process-killers pointed at the daemon by name.
_KILLERS = ("pkill", "killall", "kill")


@dataclass(frozen=True)
class PlaneTargetVerdict:
    """Fixed shape, always returned — never a bare bool.

    ``targets_plane`` is the decision; ``reason`` explains it in the caller's
    own terms so a 202 can say WHY it was scheduled rather than run. A caller
    must not have to guess which field exists on a given call.
    """

    targets_plane: bool
    reason: str | None = None


def _basename(arg: str) -> str:
    return os.path.basename(arg.strip()) if arg else ""


def targets_listen_plane(argv: list[str] | tuple[str, ...]) -> PlaneTargetVerdict:
    """Would running ``argv`` inline kill the daemon serving this request?

    Recognised, in order:

    1. a service manager (``systemctl`` / ``service``) whose argv carries a
       disruptive verb AND names the ``sac-listen`` unit;
    2. the ``sac listen`` CLI with a disruptive verb (``sac listen restart``);
    3. a killer (``pkill`` / ``killall`` / ``kill``) whose pattern names the
       daemon.

    Anything else is ``targets_plane=False`` — including plain reads such as
    ``systemctl status sac-listen``, which are safe to run inline and must NOT
    be deferred (deferring a status query would answer 202 to a question that
    wanted an answer).
    """
    if not argv:
        return PlaneTargetVerdict(False)
    args = [a for a in argv if isinstance(a, str)]
    if not args:
        return PlaneTargetVerdict(False)
    head = _basename(args[0])
    lowered = [a.lower() for a in args]
    joined = " ".join(lowered)

    names_unit = any(tok in joined for tok in _LISTEN_UNIT_TOKENS)
    has_verb = any(v in lowered for v in _DISRUPTIVE_VERBS)

    if head in _SERVICE_MANAGERS and names_unit and has_verb:
        return PlaneTargetVerdict(
            True,
            f"{head} would {', '.join(v for v in _DISRUPTIVE_VERBS if v in lowered)} "
            "the sac-listen unit that is serving this request",
        )

    # `sac listen restart` / `sac listen stop` — the CLI form scitex-storage
    # reported on 2026-07-28 (it returned HTTP 500 while SUCCEEDING).
    if head.startswith("sac") and "listen" in lowered and has_verb:
        return PlaneTargetVerdict(
            True,
            "`sac listen` with a disruptive verb would end the daemon serving "
            "this request",
        )

    if head in _KILLERS and names_unit:
        return PlaneTargetVerdict(
            True,
            f"{head} names the sac-listen daemon serving this request",
        )
    if head in _KILLERS and "sac listen" in joined:
        return PlaneTargetVerdict(
            True,
            f"{head} names the sac listen daemon serving this request",
        )

    return PlaneTargetVerdict(False)
