"""Deployed-vs-declared detector: does the unit RUN what the source INTENDS?

The axis :mod:`scitex_agent_container._jobs_audit` deliberately leaves
out. That module answers "is this declaration reachable at all?" from the
repo tree; this one answers the question underneath the two ``ExecStart``
incidents we found BY HAND, both by running the same comparison in a
terminal and eyeballing it::

    systemctl --user show -p ExecStart --value <unit>   # what ACTUALLY runs
    vs. what the JobSpec source intends

Nothing ran that comparison on a schedule, so both instances were found
by luck. A hand repair restores the STATE and leaves the SYSTEM able to
re-enter it; for anything recurring, the deliverable is the DETECTOR.

The two measured instances
==========================
1. ``sac.accounts-refresh`` — the generated unit read
   ``ExecStart=/usr/bin/env sac accounts refresh ...``, which is
   ``resolve_execstart``'s rule-3 LAST RESORT (interpreter sibling-bin
   AND ambient PATH both missed). A systemd ``--user`` unit gets a
   minimal PATH, so that form fails at ``status=127``. It kept working
   only because a hand-added ``override.conf`` drop-in pinned the
   absolute path — safety living in unmanaged host state, which a
   regeneration silently discards.
2. The same shape upstream in scitex-todo
   (``scitex-todo.wake-watcher.service``, ``/usr/bin/env scitex-todo``,
   crash-looping at 127 for ~12h) — the bug that motivated
   ``resolve_execstart`` in the first place.

Why a divergence is worth a shout in BOTH directions
====================================================
Resolved != intended means exactly one of two things, and both are worth
knowing:

* an UNMANAGED LOCAL OVERRIDE — someone hand-patched the live unit. It
  may well be a correct patch (instance 1 was!), but it is invisible to
  every reader of the source and dies at the next regeneration.
* a GENERATOR BUG — ``ecosystem up`` wrote something other than what the
  JobSpec asked for.

Three states, never two
=======================
Same doctrine as :mod:`scitex_agent_container._jobs_audit`, and the
reason this lives as a runnable check rather than a CI test: CI cannot
see the fleet host's ``systemctl --user`` at all, so a check that ran
there could only ever report "could not ask". :class:`ExecVerdict`
therefore carries UNKNOWN as a first-class outcome — a check that cannot
distinguish "matches" from "could not ask" is not a check, it is a green
light with no bulb behind it. UNKNOWN is never folded into MATCH.

``NOT_INSTALLED`` is likewise its own verdict, not a divergence: several
sac JobSpecs are declared-but-deliberately-not-deployed (the
``restart-login-expired-agents`` / ``heal-agent-auth`` deploy gate), and
calling those a divergence would train the reader to ignore the report.

Reproducibility of the INTENT is itself a finding
=================================================
The expected ExecStart comes from asking the GENERATOR
(``resolve_execstart``), never from re-implementing it — a checker that
states its own opinion of what the generator ought to emit is a
declaration with no live counterpart, i.e. the disease
:mod:`scitex_agent_container._jobs_audit` exists to detect.

But ``resolve_execstart`` is only DETERMINISTIC for an absolute head,
which it passes through verbatim. For a BARE head it resolves against
``sys.executable``'s sibling bin and then the ambient PATH — so it can
legitimately return a different answer in the checking interpreter than
it did in the generating one, and a mismatch would prove nothing. Those
jobs get :data:`ExecVerdict.UNVERIFIABLE`, which names the absolute-head
fix rather than pretending to a verdict. On the host this was written
for, a bare ``sac`` is genuinely ambiguous: seven installs, five
versions.

No ``2>/dev/null``
==================
stderr is captured and REPORTED, never discarded — see
:func:`~._probe.query_unit`. Discarding it discards the only channel that
reports the failure you did not anticipate; that exact pattern hid a dead
cron job on this host for 49 days.

Reports, never repairs
======================
There is no write path in this package. Silently rewriting host state
someone else may own is worse than naming the divergence — the
``override.conf`` in instance 1 is a live example of a hand-patch that
was RIGHT, and an auto-repair would have reverted it.
"""

from __future__ import annotations

import shutil
import subprocess

from ._model import ExecFinding, ExecStartReport, ExecVerdict, UnitState
from ._probe import (
    QUERY_TIMEOUT_SEC,
    commands_equal,
    parse_show_output,
    query_unit,
    unit_name_for,
)
from ._verdict import audit_job

#: sac owns every job whose name is prefixed ``sac.``.
SAC_PREFIX = "sac."

#: JobSpec kinds that materialise a systemd unit with an ``ExecStart``.
#: ``cron`` jobs become crontab lines and have no unit to interrogate.
SYSTEMD_KINDS = frozenset({"timer", "service"})


def _resolve_execstart_fn():
    """Locate scitex-dev's REAL ``resolve_execstart``, or return None.

    Public path first. As of scitex-dev 0.31.1 the function is not
    re-exported from ``scitex_dev.jobs``, only defined in
    ``scitex_dev.jobs._systemd`` — the module that also renders the unit,
    so it IS the generator whose intent we want. We fall back to it rather
    than re-implement the resolution rules here: a checker that states its
    own opinion of what the generator ought to emit would stay green while
    production drifted underneath it.

    A private import is a real coupling, so it is contained to this one
    function and BOTH failures degrade to None -> UNKNOWN, never to a
    divergence. The clean fix belongs upstream (export it publicly); until
    then this is the only way to ask the generator instead of guessing.
    """
    try:
        from scitex_dev.jobs import resolve_execstart

        return resolve_execstart
    except ImportError:  # stx-allow: fallback (reason: not re-exported publicly as of scitex-dev 0.31.1 — try the defining module)
        pass
    try:
        from scitex_dev.jobs._systemd import resolve_execstart

        return resolve_execstart
    except ImportError:  # stx-allow: fallback (reason: old scitex-dev has no jobs contract — UNKNOWN, not a divergence)
        return None


def _intended_execstart(job) -> str | None:
    """Ask the GENERATOR what it intends. Never re-implement it."""
    fn = _resolve_execstart_fn()
    if fn is None:
        return None
    return fn(job.command, venv=getattr(job, "venv", None))


def _declared_systemd_jobs(prefix: str) -> list:
    from scitex_agent_container._jobs_plugin import provide_jobs

    return [
        j
        for j in provide_jobs()
        if j.name.startswith(prefix) and j.kind in SYSTEMD_KINDS
    ]


def audit_execstart(
    *, prefix: str = SAC_PREFIX, runner=subprocess.run, which=shutil.which
) -> ExecStartReport:
    """Compare every sac-declared unit's RESOLVED ExecStart to its intent.

    ``runner`` / ``which`` are seams for tests that drive a real
    subprocess against a fixture ``systemctl`` on disk — a fake systemctl
    that is a real executable file, not a mock.
    """
    findings = [
        audit_job(
            job,
            state=query_unit(unit_name_for(job), runner=runner, which=which),
            intended=_intended_execstart(job),
        )
        for job in _declared_systemd_jobs(prefix)
    ]
    return ExecStartReport(findings=tuple(findings))


__all__ = [
    "QUERY_TIMEOUT_SEC",
    "SAC_PREFIX",
    "SYSTEMD_KINDS",
    "ExecFinding",
    "ExecStartReport",
    "ExecVerdict",
    "UnitState",
    "audit_execstart",
    "audit_job",
    "commands_equal",
    "parse_show_output",
    "query_unit",
    "unit_name_for",
]
