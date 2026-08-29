"""Turn a refusal into a work list — ordered by what to DO, root causes named once.

The operator's requirement, 2026-08-11: 「なるべく多くのヒントを1回で出す」— emit as
many hints as possible in ONE pass. :mod:`_relocate_preflight` already runs every
check regardless of earlier failures, so the hints all exist; this module is about
the two ways a complete list still fails its reader.

ORDER. A report ordered by check index is the code's structure leaking into the
operator's afternoon. He works by action: what the target must be given, then what
has to travel with the agent, then what is a spec edit, then what is still
unmeasured. Those are four different places to stand, and grouping by them is the
difference between one trip to a machine and four.

ROOT CAUSES. An unreachable target turns eleven checks UNKNOWN, and printing
eleven problems where there is one is worse than printing none: the real cause is
buried in its own consequences. The grouping is not a guess — every fact gathered
through a failed transport carries the SAME reason string (:mod:`_relocate_probe`
keeps the exception text per fact), so identical reasons ARE one cause, and that
is the whole rule. Two checks that failed for genuinely different reasons never
merge, because their text differs.

WHAT A CONSEQUENCE IS NOT. A check swallowed by a root cause is not dropped and
not downgraded — it is listed BY NAME under the cause that blocked it, and it
still blocks. This is the three-valued rule applied to the dependency itself: "I
could not check whether the port is free, because the host never answered" is an
unknown with a known reason, not a pass and not a fail.

Pure: a report and a dict of probe errors in, dataclasses out. The renderer turns
these into lines; nothing here prints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ._relocate_bind_kind import (
    ACTION_CARRY,
    ACTION_DECIDE,
    ACTION_PROVISION,
    classify_binds,
)
from ._relocate_checks import (
    CHECK_BINDS,
    CHECK_CARD_STORE,
    CHECK_CREDENTIALS,
    CHECK_HUB_FROM_TARGET,
    CHECK_IMAGE,
    CHECK_PORTS,
    CHECK_REACHABLE,
    CHECK_RUNTIME,
    CHECK_SCHEMA,
    CHECK_SESSION,
    CHECK_SOURCE_WORK,
)
from ._relocate_checks_late import CHECK_LEASE, CHECK_TARGET_START
from ._relocate_checks_sac import CHECK_SAC_PRESENT
from ._relocate_checks_spec import CHECK_CARD_STORE_DSN, CHECK_GROUPS, CHECK_WORKDIR
from ._relocate_preflight_facts import Check, PreflightReport

__all__ = [
    "ACTION_CARRY",
    "ACTION_DECIDE",
    "ACTION_MEASURE",
    "ACTION_ORDER",
    "ACTION_PROVISION",
    "ACTION_SPEC",
    "CHECK_FACTS",
    "Plan",
    "PlanItem",
    "RootCause",
    "build_plan",
    "why_for_check",
]

#: A spec edit — the fix is in a file, not on a machine.
ACTION_SPEC: Final = "correct the spec"
#: Nothing is known to be wrong; something is not known at all.
ACTION_MEASURE: Final = "go and measure"

#: The order the operator works in. Provisioning first because it is usually
#: somebody standing at another machine; measuring last because an unknown is not
#: yet a task, it is a question.
ACTION_ORDER: Final = (
    ACTION_PROVISION,
    ACTION_CARRY,
    ACTION_SPEC,
    ACTION_DECIDE,
    ACTION_MEASURE,
)

#: Which FACTS feed each CHECK. ``gather_target_facts`` keys its failures by fact
#: name and the report is written in checks; several are named differently on the
#: two sides, and keying only by check name silently drops exactly those reasons —
#: they are present, correct, and never shown (measured 2026-08-09 against a
#: busybox NAS, where four of five unknowns lost their explanation on the way to
#: the screen).
CHECK_FACTS: Final[dict[str, tuple[str, ...]]] = {
    CHECK_REACHABLE: ("reachable",),
    CHECK_IMAGE: ("image_present",),
    CHECK_BINDS: ("missing_bind_sources",),
    CHECK_WORKDIR: ("missing_workdir_paths",),
    CHECK_CARD_STORE_DSN: ("card_store_url",),
    CHECK_CARD_STORE: ("card_store_reachable", "card_store_url"),
    CHECK_CREDENTIALS: (
        "credential_expires_in_s",
        "credential_refresh_token_present",
    ),
    CHECK_RUNTIME: ("supported_runtimes",),
    CHECK_SCHEMA: ("rejected_spec_keys",),
    CHECK_PORTS: ("ports_in_use",),
    CHECK_GROUPS: ("target_resolved_groups",),
    CHECK_HUB_FROM_TARGET: ("hub_reachable_from_target",),
    CHECK_SAC_PRESENT: ("sac_usable_path", "sac_on_path", "sac_resolved_path"),
    CHECK_TARGET_START: ("spec_source_drift",),
    # Gathered locally rather than over ssh, so their failures are keyed by the
    # check's own name; listed so the map covers every check and a reader does
    # not have to wonder whether the omission means something.
    CHECK_SOURCE_WORK: ("source_repos",),
    CHECK_SESSION: ("source_transcripts",),
    CHECK_LEASE: ("lease",),
}

#: What a FAILING check asks the operator to do. Unknowns ignore this table
#: entirely — an unmeasured check is a question, whatever it is about.
_FAIL_ACTION: Final[dict[str, str]] = {
    CHECK_REACHABLE: ACTION_PROVISION,
    CHECK_IMAGE: ACTION_PROVISION,
    CHECK_WORKDIR: ACTION_PROVISION,
    CHECK_CARD_STORE: ACTION_PROVISION,
    CHECK_CREDENTIALS: ACTION_PROVISION,
    CHECK_PORTS: ACTION_PROVISION,
    CHECK_HUB_FROM_TARGET: ACTION_PROVISION,
    CHECK_SAC_PRESENT: ACTION_PROVISION,
    CHECK_TARGET_START: ACTION_PROVISION,
    CHECK_CARD_STORE_DSN: ACTION_SPEC,
    CHECK_RUNTIME: ACTION_SPEC,
    CHECK_SCHEMA: ACTION_SPEC,
    CHECK_GROUPS: ACTION_SPEC,
    CHECK_SOURCE_WORK: ACTION_CARRY,
    CHECK_SESSION: ACTION_CARRY,
    # The stored lease names a host, and clearing it is work on THAT host —
    # which is why it is provisioning rather than a spec edit.
    CHECK_LEASE: ACTION_PROVISION,
}

#: Checks measured on the machine being LEFT. Naming the vantage point is not
#: decoration: "path absent" means opposite things depending on which host was
#: asked, and a hint without its vantage point sends people to the wrong machine.
_SOURCE_CHECKS: Final = frozenset({CHECK_SOURCE_WORK, CHECK_SESSION, CHECK_LEASE})


@dataclass(frozen=True)
class PlanItem:
    """One thing to do, with the vantage point that makes it meaningful."""

    action: str
    check: str
    verdict: str
    what: str
    where: str
    fix: str


@dataclass(frozen=True)
class RootCause:
    """One failure that made several checks unanswerable, with their names."""

    reason: str
    checks: tuple[str, ...]

    @property
    def summary(self) -> str:
        return (
            f"{len(self.checks)} checks could not be measured for one reason: "
            f"{self.reason}"
        )


@dataclass(frozen=True)
class Plan:
    """Everything blocking this relocation, ordered as work rather than as code."""

    causes: tuple[RootCause, ...] = ()
    items: tuple[PlanItem, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.causes and not self.items

    def by_action(self) -> tuple[tuple[str, tuple[PlanItem, ...]], ...]:
        """Items bucketed in :data:`ACTION_ORDER`, empty buckets omitted."""
        out = []
        for action in ACTION_ORDER:
            members = tuple(i for i in self.items if i.action == action)
            if members:
                out.append((action, members))
        return tuple(out)


def why_for_check(check_name: str, errors: dict[str, str]) -> str:
    """The probe failure behind ``check_name``, by its own key or its facts'.

    The check's own name wins, so a caller that already keyed by check name keeps
    working; the fact names are the fallback that makes the adapter's reasons
    reach the reader.
    """
    direct = errors.get(check_name)
    if direct:
        return direct
    for fact in CHECK_FACTS.get(check_name, ()):
        reason = errors.get(fact)
        if reason:
            return reason
    return ""


def _bind_items(check: Check, workdir: str, from_host: str, to_host: str):
    """Explode a binds failure into one item per ACTION, not one per check.

    A single check produced paths that need provisioning AND paths that must
    travel. Reporting them under one heading is the exact collapse this feature
    was asked to undo, so the plan splits them and the check keeps its single
    verdict.
    """
    paths = _paths_from_detail(check.detail)
    classified = classify_binds(paths, workdir=workdir, from_host=from_host)
    for action in (ACTION_PROVISION, ACTION_CARRY, ACTION_DECIDE):
        for bind in (b for b in classified if b.action == action):
            yield PlanItem(
                action=action,
                check=check.name,
                verdict="FAIL",
                what=f"{bind.path} ({bind.kind}) — {bind.because}",
                where=to_host,
                fix=bind.fix,
            )


def _paths_from_detail(detail: str) -> tuple[str, ...]:
    """The paths out of ``bind sources absent on <host>: /a, /b``.

    Parsed from the detail rather than re-read from the facts because a plan is
    built from a REPORT — the facts are gone by then, and threading them through
    would let the two drift into describing different paths.
    """
    _, _, listed = detail.partition(": ")
    return tuple(p.strip() for p in listed.split(",") if p.strip())


def build_plan(
    report: PreflightReport,
    *,
    errors: dict[str, str] | None = None,
    workdir: str = "",
    from_host: str = "",
) -> Plan:
    """Everything blocking ``report``, grouped by cause and ordered by action.

    Failures come first within the ordering because they are known work; unknowns
    land in :data:`ACTION_MEASURE` last. Both block — that decision belongs to
    :data:`.._relocate_preflight_facts.UNKNOWN_BLOCKS_RELOCATION` and is not
    re-made here; this module only decides how to SAY it.
    """
    errors = errors or {}
    where = report.to_host

    by_reason: dict[str, list[Check]] = {}
    for check in report.unknown:
        reason = why_for_check(check.name, errors)
        if reason:
            by_reason.setdefault(reason, []).append(check)

    causes = tuple(
        RootCause(reason=reason, checks=tuple(c.name for c in checks))
        for reason, checks in by_reason.items()
        if len(checks) > 1
    )
    swallowed = {name for cause in causes for name in cause.checks}

    items: list[PlanItem] = []
    for check in report.failed:
        if check.name == CHECK_BINDS:
            items += list(_bind_items(check, workdir, from_host, where))
            continue
        items.append(
            PlanItem(
                action=_FAIL_ACTION.get(check.name, ACTION_DECIDE),
                check=check.name,
                verdict="FAIL",
                what=check.detail,
                where=from_host or where if check.name in _SOURCE_CHECKS else where,
                fix=check.hint,
            )
        )
    for check in report.unknown:
        if check.name in swallowed:
            continue
        reason = why_for_check(check.name, errors)
        items.append(
            PlanItem(
                action=ACTION_MEASURE,
                check=check.name,
                verdict="UNKNOWN",
                what=check.detail + (f" (probe error: {reason})" if reason else ""),
                where=from_host or where if check.name in _SOURCE_CHECKS else where,
                fix=check.hint,
            )
        )
    return Plan(causes=causes, items=tuple(items))
