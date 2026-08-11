"""Inert-feature detector — a DECLARATION with no LIVE counterpart.

The pathology this module exists to make LOUD, in its own words from
``_jobs_plugin.provide_jobs``: *"shipped but scheduled nowhere, it was an
inert alarm."* We diagnosed it once, in a comment, and kept doing it. A
postmortem in a comment is not a countermeasure — this is.

The shape is always a DANGLING HALF-PAIR: something is declared, and the
counterpart that would make the declaration DO anything does not exist.
Four measured instances in one night (2026-07-17), each with a PR, tests,
and often an ADR — none of which ever ran:

1. ``sac agents twin`` — ``derive_twin_spec`` wrote ``spec.env`` at top
   level, which v3 validation REJECTS, so no twin ever started. 29 green
   tests, because the FIXTURE used a spec shape no real spec has and the
   suite never ran the validator.
2. ``auth-heal`` computes a ``screen`` verdict and persists it nowhere, so
   presence-ALIVE always wins and a dead agent reads healthy.
3. ``restart.policy`` is a spec field with no enforcer in ~93 specs.
4. ``auto-merge-to-develop`` never FAILED; it was never TRIGGERED.

Why the checker lives in the test suite, not in a new mechanism
===============================================================
A checker THAT NOBODY RUNS is the fifth instance of the very disease. So
this module is consumed by ``tests/scitex_agent_container/
test__jobs_audit.py``, which runs in ``pytest-matrix-on-ubuntu-py*`` — a
REQUIRED status check on both ``develop`` and ``main``. It is deliberately
NOT wired into ``quality-audit-on-ubuntu-latest.yml``: every step there is
``continue-on-error: true``, so a checker hung off it could never go red,
which is precisely how you ship instance #5 with a straight face.

Three states, never two
=======================
:class:`Verdict` has LIVE / INERT / **UNKNOWN**. "I cannot tell whether a
counterpart exists" is UNKNOWN — never INERT. A false INERT that gets
someone to delete a working feature is worse than the disease it claims to
cure. Only positive evidence of NO counterpart yields INERT.

What this module covers — and what it does NOT
==============================================
COVERED, deterministically, from the repo tree alone:

* :data:`Form.DISCOVERY` — a JobSpec sac declares that the REAL
  ``discover_jobs()`` cannot reach. ``discover_jobs`` swallows a raising
  provider with only a ``logging.warning``, so one bad JobSpec silently
  drops sac's ENTIRE provider — all four timers vanish and nothing turns
  red. That is instance-shaped and invisible today.
* :data:`Form.CONSUMER` — a ``kind`` sac declares that no ``sac dev``
  consumer group can ever list, and a consumer group that filters on a
  ``kind`` outside ``ALLOWED_KINDS`` (i.e. one that can never match
  anything, ever).
* :data:`Form.GROUP_IS_NOT_ITS_KIND` — the ecosystem grammar is
  ``dev <kind> <verb>``, so a non-deprecated ``sac dev`` group name that
  is not itself a legal kind, or that filters on anything other than
  exactly its own name, has re-introduced the two-axis confusion that
  caused the outage in the first place. This is the form that makes the
  original bug UNREPRESENTABLE rather than merely fixed.
* :data:`Form.ALIAS_ONLY_KIND` — a kind reachable only through a
  DEPRECATED alias. The alias has a removal date, so such a kind has a
  scheduled loss of its CLI: the disease with a calendar attached.

NOT COVERED — stated plainly, because a checker that silently covers less
than it appears to is the same disease wearing a lab coat:

* **Declared vs DEPLOYED.** Whether ``sac.fleet-reconcile`` is an
  installed, enabled, running systemd ``--user`` timer on the fleet host
  is the question behind instances 2 and 3, and it is STRUCTURALLY
  unanswerable from CI: the suite runs in a SIF on a Spartan node that has
  no access to the fleet host's ``systemctl --user``. Answering it from
  the JobSpec SOURCE instead is the exact trap that produced a P0
  diagnosis off a 4-day-stale schedule (source said ``0 */2 * * *`` while
  the deployed unit said ``OnUnitActiveSec=10min``). So this module does
  not guess: the deployed axis is simply out of scope here rather than
  answered wrongly.
* **Workflow triggers** (instance 4) and **spec-shape validation**
  (instance 1) — other owners are live on those files.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    """Three states. UNKNOWN is not a soft INERT — it is a refusal to guess."""

    LIVE = "live"
    INERT = "inert"
    UNKNOWN = "unknown"


class Form(str, Enum):
    """The half-pair shapes this module can check deterministically."""

    DISCOVERY = "declared-job-unreachable-by-discover_jobs"
    CONSUMER = "declared-kind-has-no-consumer"
    IMPOSSIBLE_KIND = "consumer-filters-on-an-impossible-kind"
    GROUP_IS_NOT_ITS_KIND = "consumer-group-name-is-not-the-kind-it-filters"
    ALIAS_ONLY_KIND = "kind-reachable-only-through-a-deprecated-alias"


@dataclass(frozen=True)
class Finding:
    """One declared capability and the verdict on its live counterpart."""

    form: Form
    subject: str
    verdict: Verdict
    detail: str

    def __post_init__(self) -> None:
        # Validate at construction so a malformed finding crashes HERE and
        # not in a report someone acts on. Same doctrine as JobSpec.
        if not isinstance(self.form, Form):
            raise ValueError(f"Finding.form must be a Form, got {self.form!r}")
        if not isinstance(self.verdict, Verdict):
            raise ValueError(f"Finding.verdict must be a Verdict, got {self.verdict!r}")
        if not self.subject:
            raise ValueError("Finding.subject must be non-empty")
        if not self.detail:
            # A verdict with no stated evidence is exactly the
            # postmortem-in-a-comment this module exists to replace.
            raise ValueError(
                f"Finding({self.subject!r}).detail must state the evidence"
            )


@dataclass(frozen=True)
class InertReport:
    """The audit result. Fails LOUD via :meth:`render`, never silently."""

    findings: tuple[Finding, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(f, Finding) for f in self.findings):
            raise ValueError("InertReport.findings must all be Finding")

    def of(self, verdict: Verdict) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.verdict is verdict)

    @property
    def inert(self) -> tuple[Finding, ...]:
        return self.of(Verdict.INERT)

    @property
    def unknown(self) -> tuple[Finding, ...]:
        return self.of(Verdict.UNKNOWN)

    def render(self) -> str:
        """A report that names each dangling pair and its evidence."""
        if not self.inert:
            return "no inert declarations found"
        lines = [
            f"{len(self.inert)} DECLARATION(S) WITH NO LIVE COUNTERPART "
            f"— shipped, but nothing executes them:",
        ]
        for f in self.inert:
            lines.append(f"  [{f.form.value}] {f.subject}")
            lines.append(f"      {f.detail}")
        if self.unknown:
            lines.append(
                f"  ({len(self.unknown)} UNKNOWN — not enough evidence to "
                f"call inert; NOT a finding, do not delete anything on it)"
            )
        return "\n".join(lines)


def audit_discovery(
    *,
    declared_names: frozenset[str],
    discovered_names: frozenset[str] | None,
) -> tuple[Finding, ...]:
    """Form DISCOVERY: can the real aggregator reach what we declare?

    ``discovered_names=None`` means discovery could not run at all (an old
    scitex-dev with no jobs contract, say) — every declaration is then
    UNKNOWN, because absence of evidence is not evidence of absence.
    """
    if discovered_names is None:
        return tuple(
            Finding(
                form=Form.DISCOVERY,
                subject=name,
                verdict=Verdict.UNKNOWN,
                detail=(
                    "discover_jobs() could not run (no jobs contract "
                    "installed) — cannot tell if this job is reachable"
                ),
            )
            for name in sorted(declared_names)
        )
    return tuple(
        Finding(
            form=Form.DISCOVERY,
            subject=name,
            verdict=Verdict.LIVE if name in discovered_names else Verdict.INERT,
            detail=(
                "reachable via the scitex_dev.jobs entry point"
                if name in discovered_names
                else (
                    "declared by provide_jobs() but ABSENT from "
                    "discover_jobs() — the entry point is unregistered, or "
                    "the provider raised and was swallowed by the "
                    "WARN-only provider isolation, dropping sac's whole "
                    "provider. Nothing schedules this job."
                )
            ),
        )
        for name in sorted(declared_names)
    )


def audit_consumers(
    *,
    declared_kinds: frozenset[str],
    group_kinds: dict[str, frozenset[str]],
    allowed_kinds: frozenset[str],
) -> tuple[Finding, ...]:
    """Form CONSUMER / IMPOSSIBLE_KIND: is anything able to read what we declare?

    Two directions, because the pair dangles from either end:

    * a consumer group filtering on a kind outside ``allowed_kinds`` can
      never match anything — the JobSpec validator rejects that kind at
      construction, so no such job can exist;
    * a declared kind no consumer group covers is a job the CLI cannot
      list, install, or uninstall.
    """
    findings: list[Finding] = []

    for group in sorted(group_kinds):
        impossible = sorted(group_kinds[group] - allowed_kinds)
        if impossible:
            findings.append(
                Finding(
                    form=Form.IMPOSSIBLE_KIND,
                    subject=f"sac dev {group}",
                    verdict=Verdict.INERT,
                    detail=(
                        f"filters on kind(s) {impossible} which are not in "
                        f"ALLOWED_KINDS {sorted(allowed_kinds)} — "
                        f"JobSpec.validate() rejects them at construction, "
                        f"so this group can never list a single job"
                    ),
                )
            )

    covered: set[str] = set()
    for kinds in group_kinds.values():
        covered |= kinds
    for kind in sorted(declared_kinds):
        findings.append(
            Finding(
                form=Form.CONSUMER,
                subject=f"kind={kind}",
                verdict=Verdict.LIVE if kind in covered else Verdict.INERT,
                detail=(
                    "listable by a sac dev group"
                    if kind in covered
                    else (
                        "sac declares job(s) of this kind but no sac dev "
                        "group filters on it — those jobs cannot be listed, "
                        "installed or uninstalled through the CLI"
                    )
                ),
            )
        )
    return tuple(findings)


def audit_group_naming(
    *,
    group_kinds: dict[str, frozenset[str]],
    allowed_kinds: frozenset[str],
    deprecated: frozenset[str],
) -> tuple[Finding, ...]:
    """Form GROUP_IS_NOT_ITS_KIND / ALIAS_ONLY_KIND: is the grammar intact?

    The ecosystem grammar is ``dev <kind> <verb>`` — the group name IS the
    ``JobSpec.kind``. Enforcing that identity is what makes the original
    outage structurally impossible rather than merely fixed: when the two
    axes are one, there is no group name that can be passed where a kind
    is expected and quietly mean something else.

    Two checks:

    * a NON-deprecated group whose name is not a legal kind, or which
      filters on anything other than exactly itself, has re-introduced
      the second axis;
    * a kind reachable ONLY through a deprecated alias is a kind that
      loses its CLI on the alias's removal date — a scheduled deletion of
      a live capability, which is the disease with a calendar attached.

    ``deprecated`` is the set of alias group names, passed in rather than
    imported here for the same reason ``group_kinds`` is: this function
    stays pure and the caller reads the real production values.
    """
    findings: list[Finding] = []

    for group in sorted(group_kinds):
        if group in deprecated:
            continue
        if group not in allowed_kinds:
            findings.append(
                Finding(
                    form=Form.GROUP_IS_NOT_ITS_KIND,
                    subject=f"sac dev {group}",
                    verdict=Verdict.INERT,
                    detail=(
                        f"group name {group!r} is not a JobSpec kind "
                        f"({sorted(allowed_kinds)}) and is not declared a "
                        "deprecated alias — the group name and the kind are "
                        "two axes again, which is the shape that hid every "
                        "sac timer from its own CLI"
                    ),
                )
            )
            continue
        if group_kinds[group] != frozenset({group}):
            findings.append(
                Finding(
                    form=Form.GROUP_IS_NOT_ITS_KIND,
                    subject=f"sac dev {group}",
                    verdict=Verdict.INERT,
                    detail=(
                        f"group {group!r} filters on "
                        f"{sorted(group_kinds[group])} rather than exactly "
                        f"['{group}'] — a kind group must mean its own name"
                    ),
                )
            )

    live_cover: set[str] = set()
    alias_cover: set[str] = set()
    for group, kinds in group_kinds.items():
        (alias_cover if group in deprecated else live_cover).update(kinds)
    for kind in sorted(alias_cover - live_cover):
        findings.append(
            Finding(
                form=Form.ALIAS_ONLY_KIND,
                subject=f"kind={kind}",
                verdict=Verdict.INERT,
                detail=(
                    f"kind {kind!r} is reachable only through a deprecated "
                    f"alias ({sorted(deprecated)}) — it loses its CLI on the "
                    "alias's removal date"
                ),
            )
        )
    return tuple(findings)


def _declared_jobs() -> list:
    from scitex_agent_container._jobs._jobs_plugin import provide_jobs

    return list(provide_jobs())


def _consumer_group_kinds() -> dict[str, frozenset[str]]:
    """The kind filter the ``sac dev`` groups ACTUALLY use.

    Imported from the consumer rather than re-declared here, on purpose.
    A checker that states its own opinion of what the consumer ought to
    filter on is itself a declaration with no live counterpart — it would
    stay green while production drifted away underneath it, which is the
    exact disease this module exists to detect. Read the real thing.
    """
    from scitex_agent_container.cli_pkg._dev_jobs import GROUP_KINDS

    return GROUP_KINDS


def _deprecated_groups() -> frozenset[str]:
    """The alias group names, read from the consumer — same doctrine."""
    from scitex_agent_container.cli_pkg._dev_jobs import DEPRECATED_GROUPS

    return frozenset(DEPRECATED_GROUPS)


def _discovered_sac_names(prefix: str) -> frozenset[str] | None:
    try:
        from scitex_dev.jobs import discover_jobs
    except ImportError:  # stx-allow: fallback (reason: old scitex-dev has no jobs contract — UNKNOWN, not INERT)
        return None
    return frozenset(j.name for j in discover_jobs() if j.name.startswith(prefix))


def _allowed_kinds() -> frozenset[str] | None:
    try:
        from scitex_dev.jobs import ALLOWED_KINDS
    except ImportError:  # stx-allow: fallback (reason: old scitex-dev has no jobs contract — UNKNOWN, not INERT)
        return None
    return frozenset(ALLOWED_KINDS)


def audit_jobs(*, prefix: str = "sac.") -> InertReport:
    """Run every covered form against the REAL declarations and consumers."""
    declared = _declared_jobs()
    declared_names = frozenset(j.name for j in declared)
    declared_kinds = frozenset(j.kind for j in declared)

    findings: list[Finding] = list(
        audit_discovery(
            declared_names=declared_names,
            discovered_names=_discovered_sac_names(prefix),
        )
    )

    allowed = _allowed_kinds()
    if allowed is None:
        findings.append(
            Finding(
                form=Form.CONSUMER,
                subject="sac dev job groups",
                verdict=Verdict.UNKNOWN,
                detail=(
                    "ALLOWED_KINDS unavailable (no jobs contract installed) "
                    "— cannot tell which kinds are legal"
                ),
            )
        )
    else:
        findings.extend(
            audit_consumers(
                declared_kinds=declared_kinds,
                group_kinds=_consumer_group_kinds(),
                allowed_kinds=allowed,
            )
        )
        findings.extend(
            audit_group_naming(
                group_kinds=_consumer_group_kinds(),
                allowed_kinds=allowed,
                deprecated=_deprecated_groups(),
            )
        )
    return InertReport(findings=tuple(findings))


__all__ = [
    "Finding",
    "Form",
    "InertReport",
    "Verdict",
    "audit_consumers",
    "audit_discovery",
    "audit_group_naming",
    "audit_jobs",
]
