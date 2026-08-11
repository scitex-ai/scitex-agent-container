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

from ._relocate_preflight import PreflightReport

__all__ = [
    "VERDICT_GO",
    "VERDICT_REFUSED",
    "VERDICT_UNKNOWN",
    "render_declared",
    "render_dry_run",
    "render_observed",
    "verdict_line",
]

VERDICT_GO = "GO"
VERDICT_REFUSED = "REFUSED"
VERDICT_UNKNOWN = "REFUSED (undetermined)"

_LABEL = {True: "PASS", False: "FAIL", None: "UNKNOWN"}

#: Which FACTS feed each CHECK. ``gather_target_facts`` keys its failures by
#: fact name, and four checks are named differently from the fact behind them
#: (``credentials_valid`` is fed by ``credential_expires_in_s``, and so on).
#: Without this map those four print a bare UNKNOWN while the reason for it sits
#: unused in the errors dict — measured 2026-08-09 against a busybox NAS, where
#: four of five unknowns lost their explanation on the way to the screen.
_CHECK_FACTS: dict[str, tuple[str, ...]] = {
    "target_reachable": ("reachable",),
    "image_present": ("image_present",),
    "binds_exist_on_target": ("missing_bind_sources",),
    "card_store_reachable": ("card_store_reachable", "card_store_url"),
    "credentials_valid": (
        "credential_expires_in_s",
        "credential_refresh_token_present",
    ),
    "runtime_supported": ("supported_runtimes",),
    "spec_schema_accepted": ("rejected_spec_keys",),
    "ports_free": ("ports_in_use",),
    "hub_reachable_from_target": ("hub_reachable_from_target",),
    "sac_present_on_target": ("sac_on_path", "sac_resolved_path"),
    # Gathered locally rather than over ssh, so its failures are keyed by the
    # check's own name; the tuple is here so the map covers every check and a
    # reader does not have to wonder whether the omission means something.
    "source_work_committed": ("source_repos",),
}


def _why(check_name: str, errors: dict[str, str]) -> str:
    """The probe failure behind ``check_name``, by its own key or its facts'.

    The check's own name wins, so a caller that already keyed by check name
    keeps working; the fact names are the fallback that makes the adapter's
    reasons reach the reader.
    """
    direct = errors.get(check_name)
    if direct:
        return direct
    for fact in _CHECK_FACTS.get(check_name, ()):
        reason = errors.get(fact)
        if reason:
            return reason
    return ""


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


def render_dry_run(
    report: PreflightReport,
    *,
    declared: dict[str, object] | None = None,
    errors: dict[str, str] | None = None,
    dry_run: bool = True,
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
    blocking = report.failed + report.unknown
    if blocking:
        lines.append("")
        lines.append("BLOCKING — fix or measure each of these, then re-run:")
        # Names and details only. The hints are already printed inline above,
        # and repeating nine identical "run the probe that supplies this fact"
        # paragraphs turns the summary — the part that gets pasted into chat —
        # into the least readable thing on screen.
        lines += [f"  - {_LABEL[c.ok]:<8} {c.name}: {c.detail}" for c in blocking]
    return lines
