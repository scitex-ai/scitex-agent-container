"""The BASE-CASE apply: sac's DECLARATIONS become ARMED units on this host.

FOUR STATES, AND CONFLATING THEM IS THE BUG
===========================================
This module exists because four different things were being called "the
job is set up", and only the first two were ever true:

* **DECLARED** — a :class:`~scitex_dev.jobs.JobSpec` exists in this repo
  (``_jobs._jobs_plugin.provide_jobs``). Nine of them, all
  ``kind="timer"``.
* **REGISTERED** — ``scitex_dev.jobs.discover_jobs()`` can FIND those
  specs, via the ``scitex_dev.jobs`` entry point declared in
  ``pyproject.toml``. Measured 2026-08-15: it finds all nine.
* **APPLIED** — a unit file (or crontab line) for the job exists on THIS
  host. Nothing in sac has ever produced one.
* **ARMED** — the applied unit is ``enabled``, i.e. it will actually
  fire. Nothing in sac has ever produced this either, and it is a
  SEPARATE step from APPLIED rather than a consequence of it.

The last point is the whole defect, and it is mechanical rather than
philosophical. scitex-dev's ``_jobs_units.do_install`` writes the unit
files and then merely PRINTS the arming command to stderr::

    click.echo("Enable with:", err=True)
    click.echo(f"  systemctl --user enable --now {enable_target(kind, job)}", err=True)

It never runs it. So "install everything" leaves a host carrying N
correct, inert unit files plus N stderr lines a human is expected to
copy-paste one at a time. On 2026-08-15 seven of ten sac timers were
sitting ``disabled`` on scitex-compute-04 — including the sweep that
restarts agents wedged behind a frozen "Login expired" banner, the
failure that had taken out three agents overnight. That is not an
accident anyone made; it is the arithmetic of the surface.

WHY THIS IS A BASE CASE AND NOT A TIMER
=======================================
The obvious fix is "declare a convergence job that re-asserts
declared-equals-armed". That fix cannot stand alone, because a
convergence timer is itself a declared job: nothing would arm IT either.
So the recursion needs a base case, and the base case must be something
that runs UNCONDITIONALLY on a host that has nothing yet — i.e. host
provisioning. :func:`apply_declared_jobs` is that step, and
``sac installation boot`` is where it is called from.

The PERIODIC half — re-asserting the invariant so hand-disables and
rebuilt hosts self-correct — is deliberately NOT declared here. One job
that converges EVERY registered leaf belongs in scitex-dev's own
provider; a copy of it in each leaf package is N copies of one job, which
is the shape ``_dev_jobs_backend`` already argues against at length.

WHAT THIS MODULE DOES NOT OWN
=============================
No ``systemctl`` call lives here. Every step is delegated through
:func:`._dev_jobs._delegate`, the single mutation seam, which resolves
and shells ``scitex-dev ecosystem <path...> <verb>``. sac decides WHICH
jobs and IN WHAT ORDER; scitex-dev decides what a unit is and how a host
is touched. That split is the fleet's standing pattern — the primitive
lives in scitex-dev, the leaf declares its specifics — and it is the
reason this file is ~200 lines instead of a second unit renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from . import _dev_jobs

#: The kinds provisioning walks, in the order it walks them.
#:
#: Read off ``_dev_jobs.GROUP_KINDS`` rather than restated, minus the
#: deprecated ``systemd`` alias — applying through an alias would apply
#: its two real kinds a second time, and the double-supervision guard in
#: scitex-dev would then refuse work that had just succeeded.
APPLY_KINDS: tuple[str, ...] = ("service", "timer", "cron")

#: The verbs that turn a declaration into a firing job, IN ORDER.
#:
#: ``install`` makes it APPLIED; ``enable`` makes it ARMED. Both, always,
#: and in this order — the entire point of this module is that the second
#: one is not optional and does not follow from the first.
APPLY_SEQUENCE: tuple[str, ...] = ("install", "enable")


@dataclass(frozen=True)
class Step:
    """One delegated verb, for one declared job, and what it returned.

    ``rc`` is the exit code the delegation produced. It is recorded per
    step rather than folded into a single boolean because "the unit was
    written but could not be enabled" and "the unit was never written"
    are different failures with different remedies, and a report that
    cannot tell them apart sends the operator to the wrong place.
    """

    kind: str
    verb: str
    job: str
    rc: int

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def __str__(self) -> str:
        state = "ok" if self.ok else f"rc={self.rc}"
        return f"{self.kind} {self.verb} {self.job}: {state}"


@dataclass(frozen=True)
class ApplyReport:
    """Everything one apply pass did, and everything it could not do."""

    steps: tuple[Step, ...] = ()
    #: ``"<kind>: <reason>"`` for a kind that was not attempted at all.
    skipped: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[Step, ...]:
        return tuple(s for s in self.steps if not s.ok)

    @property
    def ok(self) -> bool:
        """True when every attempted step succeeded and none was skipped.

        A skip counts against ``ok`` on purpose. A pass that could not
        even look at a kind has not established the invariant for it, and
        reporting that as success is the exact "the success value is also
        the didn't-check value" shape this card is about.
        """
        return not self.failed and not self.skipped

    @property
    def jobs_armed(self) -> tuple[str, ...]:
        """Jobs whose ``enable`` step returned 0 — the ARMED set."""
        return tuple(
            s.job for s in self.steps if s.verb == "enable" and s.ok
        )

    @property
    def jobs_applied(self) -> tuple[str, ...]:
        """Jobs whose ``install`` step returned 0 — the APPLIED set."""
        return tuple(
            s.job for s in self.steps if s.verb == "install" and s.ok
        )

    def summary(self) -> str:
        """A one-line verdict that never says more than it measured."""
        n_jobs = len({s.job for s in self.steps})
        parts = [
            f"{n_jobs} declared job(s): "
            f"{len(self.jobs_applied)} applied, {len(self.jobs_armed)} armed"
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} step(s) FAILED")
        if self.skipped:
            parts.append(f"{len(self.skipped)} kind(s) skipped")
        return "; ".join(parts)


def apply_verbs(kind: str) -> tuple[str, ...]:
    """The apply sequence restricted to verbs ``kind`` actually has.

    Reads :data:`._dev_jobs.GROUP_VERBS` — the SSoT that is already
    matched verbatim to scitex-dev's counterpart — rather than restating
    it. The live consequence today is ``kind="service"``, which has no
    ``enable`` verb (scitex-dev #566 does not serve one, so exposing it
    would be a permanent exit-4): a service is APPLIED by this path and
    NOT ARMED by it, and the report says so instead of implying
    otherwise.
    """
    have = _dev_jobs.GROUP_VERBS.get(kind, ())
    return tuple(v for v in APPLY_SEQUENCE if v in have)


def apply_kind(
    kind: str,
    *,
    yes: bool,
    dry_run: bool = False,
    delegate: Callable[..., int] | None = None,
    echo: Callable[[str], None] | None = None,
) -> ApplyReport:
    """Apply + arm every job sac declares of one ``kind``.

    ``delegate`` defaults to :func:`._dev_jobs._delegate`, the single
    mutation seam. A test passes its own to capture the exact
    ``(kind, verb, name, yes, dry_run)`` tuples without letting a real
    ``scitex-dev`` rewrite the host's units.
    """
    run = delegate if delegate is not None else _dev_jobs._delegate
    log = echo if echo is not None else (lambda _s: None)

    try:
        jobs = _dev_jobs._load_sac_jobs(_dev_jobs.GROUP_KINDS[kind])
    except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — record a named skip, never a silent success)
        reason = f"{kind}: scitex-dev is too old to serve `scitex_dev.jobs`"
        log(reason)
        return ApplyReport(skipped=(reason,))

    if not jobs:
        # Not a skip: there is genuinely nothing of this kind to apply, so
        # the invariant holds vacuously. Recording it as skipped would
        # make a correct host look unconverged forever.
        return ApplyReport()

    verbs = apply_verbs(kind)
    if not verbs:
        reason = f"{kind}: no apply verbs available on this grammar"
        log(reason)
        return ApplyReport(skipped=(reason,))

    steps: list[Step] = []
    for job in jobs:
        for verb in verbs:
            rc = _run_step(run, kind, verb, job.name, yes=yes, dry_run=dry_run)
            step = Step(kind=kind, verb=verb, job=job.name, rc=rc)
            steps.append(step)
            log(f"  {step}")
    return ApplyReport(steps=tuple(steps))


def _run_step(
    run: Callable[..., int],
    kind: str,
    verb: str,
    name: str,
    *,
    yes: bool,
    dry_run: bool,
) -> int:
    """Delegate one verb, converting an abort into a recorded exit code.

    ``_delegate`` raises ``SystemExit(4)`` when the installed scitex-dev
    cannot serve a verb. Letting that propagate would abandon the
    remaining jobs mid-pass and leave the host in a state nobody
    recorded — so it is caught, recorded, and the pass continues. An
    unarmed job that is REPORTED is recoverable; an unarmed job nobody
    counted is what this card is about.
    """
    try:
        return int(run(kind, verb, name, yes, dry_run))
    except SystemExit as exc:  # stx-allow: fallback (reason: a verb the installed scitex-dev cannot serve must not abandon the remaining jobs)
        code = exc.code
        return code if isinstance(code, int) else 1


def apply_declared_jobs(
    *,
    yes: bool,
    dry_run: bool = False,
    kinds: Iterable[str] = APPLY_KINDS,
    delegate: Callable[..., int] | None = None,
    echo: Callable[[str], None] | None = None,
) -> ApplyReport:
    """Apply + arm EVERY job sac declares, across every kind.

    This is the collective, idempotent, base-case step host provisioning
    calls. It is safe to re-run: scitex-dev's ``install`` refuses to
    write over an existing supervisor rather than creating a second one,
    and ``enable`` on an already-enabled unit is a no-op.
    """
    steps: list[Step] = []
    skipped: list[str] = []
    for kind in kinds:
        report = apply_kind(
            kind, yes=yes, dry_run=dry_run, delegate=delegate, echo=echo
        )
        steps.extend(report.steps)
        skipped.extend(report.skipped)
    return ApplyReport(steps=tuple(steps), skipped=tuple(skipped))


__all__ = [
    "APPLY_KINDS",
    "APPLY_SEQUENCE",
    "ApplyReport",
    "Step",
    "apply_declared_jobs",
    "apply_kind",
    "apply_verbs",
]
