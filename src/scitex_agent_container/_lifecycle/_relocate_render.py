"""Render a relocation dry run so DECLARED and OBSERVED never share a column.

The operator's requirement, 2026-08-08: 「定義されているのと、今動いているのって
違うんで」 — what a spec DECLARES and what a host actually SHOWS are different
facts, and collapsing them into one column is how `sac agents list` ends up
reporting a running agent as `defined`. This renderer keeps them in separate
sections so a reader cannot mistake one for the other.

THREE OUTCOMES, THREE WORDS. ``PASS`` / ``FAIL`` / ``UNKNOWN``, never two.
:mod:`_relocate_preflight` is three-valued precisely so "I could not tell"
survives to the operator, and a renderer that prints unknowns as failures (or
worse, omits them) throws that away at the last step. They call for different
actions: a FAIL is something to fix, an UNKNOWN is something to go and measure.

WHY THE PROBE ERROR IS PRINTED NEXT TO THE UNKNOWN. ``gather_target_facts``
keeps the exception text rather than discarding it, so the line can say
``credentials_valid: UNKNOWN (SSHTimeout: ...)`` instead of the bare "was not
observed" the checks alone can offer. Without it the operator knows a fact is
missing but not why, which turns a five-second fix into an investigation.

That lookup goes through :data:`_CHECK_FACTS`, because the errors are keyed by
FACT and the report is written in CHECKS, and four of the nine are named
differently on the two sides. Keying only by check name silently drops exactly
those four reasons — they are present, correct, and never shown.

Pure: strings in, strings out. No click, no console, no colour — the caller
decides how to paint them, and tests can assert on exact lines.
"""

from __future__ import annotations

from ._relocate_plan import Plan, build_plan, why_for_check
from ._relocate_preflight import PreflightReport

__all__ = [
    "VERDICT_GO",
    "VERDICT_REFUSED",
    "VERDICT_UNKNOWN",
    "render_declared",
    "render_dry_run",
    "render_observed",
    "render_plan",
    "verdict_line",
]

VERDICT_GO = "GO"
VERDICT_REFUSED = "REFUSED"
VERDICT_UNKNOWN = "REFUSED (undetermined)"

_LABEL = {True: "PASS", False: "FAIL", None: "UNKNOWN"}

def _why(check_name: str, errors: dict[str, str]) -> str:
    """The probe failure behind ``check_name``. See :func:`._relocate_plan.why_for_check`.

    The check-to-fact map lives with the plan rather than here, because the plan
    needs it to recognise several unknowns as ONE root cause. Two copies of that
    map would eventually disagree, and the symptom would be a reason printed
    inline while the same reason failed to group.
    """
    return why_for_check(check_name, errors)


def render_declared(declared: dict[str, object]) -> list[str]:
    """The spec's own claims, labelled as claims.

    Printed BEFORE the observations and explicitly marked unverified, because
    this section is the input to the checks, not evidence about the target. A
    reader who skims should not come away thinking the runtime was confirmed
    merely because it appeared in the output.
    """
    lines = ["DECLARED (from the spec — not verified by this run)"]
    if not declared:
        lines.append("  (nothing declared)")
        return lines
    width = max(len(k) for k in declared)
    for key, value in declared.items():
        lines.append(f"  {key:<{width}}  {_fmt(value)}")
    return lines


def _fmt(value: object) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(none)"
    return str(value)


def render_observed(
    report: PreflightReport, errors: dict[str, str] | None = None
) -> list[str]:
    """One line per check, plus the hint and probe error where they exist.

    Every check is printed, including the passes. A dry run that shows only
    problems leaves the operator unable to tell "this check passed" from "this
    check was never run" — and that ambiguity is the exact defect the three
    outcomes exist to remove.
    """
    errors = errors or {}
    lines = [f"OBSERVED (probed on {report.to_host})"]
    width = max((len(c.name) for c in report.checks), default=0)
    for check in report.checks:
        lines.append(f"  {_LABEL[check.ok]:<8} {check.name:<{width}}  {check.detail}")
        why = _why(check.name, errors)
        if why:
            lines.append(f"           probe error: {why}")
        if check.ok is not True and check.hint:
            lines.append(f"           -> {check.hint}")
    return lines


def verdict_line(report: PreflightReport) -> str:
    """One sentence a reader can act on, with the counts that justify it.

    An unknown refuses exactly as firmly as a failure — but says so in its own
    words, so the operator knows to go and measure rather than to go and fix.
    """
    n_fail = len(report.failed)
    n_unknown = len(report.unknown)
    if report.ok is True:
        return (
            f"VERDICT  {VERDICT_GO} — every check passed ({len(report.checks)} checks)"
        )
    if report.ok is False:
        tail = f", {n_unknown} could not be determined" if n_unknown else ""
        return f"VERDICT  {VERDICT_REFUSED} — {n_fail} failed{tail}"
    return (
        f"VERDICT  {VERDICT_UNKNOWN} — nothing failed, but {n_unknown} "
        "could not be determined; an unmeasured check is not a passing one"
    )


def render_plan(plan: Plan) -> list[str]:
    """The work list: root causes once, then items grouped by what to DO.

    Replaces a flat "BLOCKING" dump ordered by check index. Two things changed
    and both were asked for: several unknowns sharing one probe failure are
    stated ONCE with the affected checks named, and the rest are bucketed by
    action so the reader can work top-down instead of re-sorting the list in his
    head.

    Every entry carries its vantage point. "the workdir does not exist" is a
    different sentence depending on which host was asked, and a fix without a
    host attached is a fix somebody applies on the wrong machine.
    """
    if plan.empty:
        return []
    lines = ["BLOCKING — every problem this run found, in the order to work them:"]
    for cause in plan.causes:
        lines.append("")
        lines.append(f"  ONE ROOT CAUSE — {cause.summary}")
        lines.append(f"    blocked: {', '.join(cause.checks)}")
        lines.append(
            "    these are not separate problems; fix this one and re-run to learn "
            "what the rest actually say"
        )
    for action, items in plan.by_action():
        lines.append("")
        lines.append(f"  [{action.upper()}]")
        for item in items:
            lines.append(f"    {item.verdict:<8} {item.check} (on {item.where})")
            lines.append(f"      what: {item.what}")
            lines.append(f"      fix:  {item.fix}")
    return lines


def render_dry_run(
    report: PreflightReport,
    *,
    declared: dict[str, object] | None = None,
    errors: dict[str, str] | None = None,
    dry_run: bool = True,
    workdir: str = "",
    from_host: str = "",
) -> list[str]:
    """The whole dry run, in the order a reader needs it.

    Header first so the agent and target are unambiguous, then what the spec
    claims, then what the host showed, then the verdict — and the verdict is
    repeated as blocking reasons at the end, because the operator asked for a
    dry run that surfaces EVERY problem in one pass rather than one per run.

    ``dry_run`` EXISTS BECAUSE THE HEADER WAS A LIE. The sentence "(nothing was
    touched)" was unconditional, so the first real relocation printed it above a
    report of 3.6 MB moved between two hosts — measured 2026-08-11 on the canary
    run. A report that misstates whether it changed anything is precisely the
    "looks exactly like success" failure this command exists to prevent, aimed
    at the reader instead of the machine. It defaults to True so a caller that
    forgets it over-warns rather than under-warns.
    """
    lines = [
        f"relocate {report.agent} -> {report.to_host}   "
        + (
            "DRY RUN (nothing was touched)"
            if dry_run
            else "EXECUTING (this run CHANGES both hosts)"
        ),
        "",
    ]
    lines += render_declared(declared or {})
    lines.append("")
    lines += render_observed(report, errors)
    lines.append("")
    lines.append(verdict_line(report))
    plan = build_plan(report, errors=errors, workdir=workdir, from_host=from_host)
    if not plan.empty:
        lines.append("")
        lines += render_plan(plan)
    return lines
