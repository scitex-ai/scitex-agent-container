"""Deciding one job's verdict from what systemd said vs what we intend.

The judgement half of the audit. The ORDER of the branches below is the
safety property: every "I could not tell" case is decided BEFORE the
comparison, so a missing input can never fall through into MATCH.
"""

from __future__ import annotations

import shlex

from ._model import ExecFinding, ExecVerdict, UnitState
from ._probe import commands_equal, unit_name_for


def audit_job(job, *, state: UnitState, intended: str | None) -> ExecFinding:
    """Decide one job's verdict.

    ``intended=None`` means the generator could not be asked what it
    intends (an old scitex-dev with no jobs contract) — UNKNOWN, never a
    divergence.
    """
    unit = unit_name_for(job)
    head = (shlex.split(job.command) or [""])[0]

    # --- the "could not tell" branches, all before any comparison -------
    if state.error and state.load_state is None:
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.UNKNOWN,
            detail=f"could not ask systemd: {state.error}",
        )
    if intended is None:
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.UNKNOWN,
            detail=(
                "cannot compute the intended ExecStart — scitex_dev.jobs "
                "resolve_execstart is unavailable (old scitex-dev)"
            ),
        )
    if state.load_state == "not-found":
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.NOT_INSTALLED,
            detail=(
                "declared but no unit is installed — expected for a job "
                "behind a deliberate deploy gate, a finding otherwise"
            ),
            intended=intended,
        )
    if state.execstart is None:
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.UNKNOWN,
            detail=(
                f"unit LoadState={state.load_state!r} but systemd reported "
                f"no ExecStart to compare"
                + (f"; stderr: {state.error}" if state.error else "")
            ),
            intended=intended,
        )
    if not head.startswith("/"):
        # The INTENT is not reproducible here: resolve_execstart resolves a
        # bare head against THIS interpreter's sibling bin and PATH, which
        # need not be the ones that generated the unit. Refuse to compare
        # rather than emit a divergence that proves nothing.
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.UNVERIFIABLE,
            detail=(
                f"declared command head {head!r} is not absolute, so the "
                f"intended ExecStart depends on which interpreter ran "
                f"`ecosystem up` and cannot be reproduced here. The unit "
                f"runs: {state.execstart!r}. Declare an absolute path to "
                f"make this job checkable — resolve_execstart passes an "
                f"absolute head through verbatim."
            ),
            intended=intended,
            resolved=state.execstart,
        )

    # --- the actual comparison ------------------------------------------
    if commands_equal(intended, state.execstart):
        return ExecFinding(
            job=job.name,
            unit=unit,
            verdict=ExecVerdict.MATCH,
            detail="unit runs exactly what the JobSpec declares",
            intended=intended,
            resolved=state.execstart,
        )
    return ExecFinding(
        job=job.name,
        unit=unit,
        verdict=ExecVerdict.DIVERGED,
        detail=(
            "the running unit does not match the declaration — either a "
            "drop-in is patching it (check "
            f"~/.config/systemd/user/{unit}.d/), or `ecosystem up` emitted "
            "something other than what the JobSpec asked for. REPORTED, "
            "NOT REPAIRED: the override may well be the correct side."
        ),
        intended=intended,
        resolved=state.execstart,
    )


__all__ = ["audit_job"]
