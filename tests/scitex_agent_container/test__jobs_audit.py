"""Tests for the inert-feature detector (``_jobs_audit``).

This file IS the gate. It runs in ``pytest-matrix-on-ubuntu-py{3.11,3.12,
3.13}``, a REQUIRED status check on both ``develop`` and ``main``, which
is the whole point: a checker nobody runs is another instance of the
disease it claims to detect, so the checker had to be hung off something
that already fails builds. It is deliberately NOT in the quality-audit
workflow — every step there is ``continue-on-error: true``.

No mocks (PA-306). The two pure functions take plain frozensets/dicts and
return dataclasses, so the "arrange" is real data, not a patched seam;
the integration tests drive the REAL ``provide_jobs`` / ``discover_jobs``
/ Click groups with the REAL ``scitex_dev.jobs`` installed.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs_audit import (  # noqa: E402
    Finding,
    Form,
    InertReport,
    Verdict,
    audit_consumers,
    audit_discovery,
    audit_jobs,
)
from scitex_agent_container.cli_pkg._dev_jobs import GROUP_KINDS  # noqa: E402

# The mapping the CLI ACTUALLY used before this was fixed: ``_load_sac_jobs``
# was called with the GROUP NAME, so each group filtered on a kind literally
# equal to its own name. Reconstructed here as data so the detector can be
# shown firing on the real historical bug rather than on a hypothetical.
_PRE_FIX_GROUP_KINDS = {
    "cron": frozenset({"cron"}),
    "systemd": frozenset({"systemd"}),
    "daemon": frozenset({"daemon"}),
}


# ---------------------------------------------------------------------------
# Form DISCOVERY — declared vs reachable by the real aggregator
# ---------------------------------------------------------------------------


def test_every_declared_job_is_reachable_by_discover_jobs() -> None:
    # Arrange — the real provider + the real entry-point aggregation.
    # `discover_jobs` swallows a raising provider with only a warning, so a
    # single bad JobSpec silently drops ALL of sac's timers and nothing
    # else in the suite would notice.
    # Act
    report = audit_jobs()
    # Assert
    assert not [f for f in report.inert if f.form is Form.DISCOVERY], report.render()


def test_declared_job_missing_from_discovery_reads_inert() -> None:
    # Arrange — declared, but the aggregator cannot see it.
    # Act
    findings = audit_discovery(
        declared_names=frozenset({"sac.ghost"}),
        discovered_names=frozenset({"sac.real"}),
    )
    # Assert
    assert findings[0].verdict is Verdict.INERT


def test_discovery_without_the_contract_reads_unknown_not_inert() -> None:
    # Arrange — discovery could not run at all. Absence of evidence is not
    # evidence of absence: a false INERT here would invite someone to
    # delete a working job, which is worse than the disease.
    # Act
    findings = audit_discovery(
        declared_names=frozenset({"sac.accounts-refresh"}),
        discovered_names=None,
    )
    # Assert
    assert findings[0].verdict is Verdict.UNKNOWN


def test_discovery_inert_finding_states_its_evidence() -> None:
    # Arrange
    findings = audit_discovery(
        declared_names=frozenset({"sac.ghost"}),
        discovered_names=frozenset(),
    )
    # Act
    detail = findings[0].detail
    # Assert — a verdict with no stated evidence is the postmortem-in-a-
    # comment this module replaces.
    assert "discover_jobs()" in detail


# ---------------------------------------------------------------------------
# Form CONSUMER — declared vs anything able to read it
# ---------------------------------------------------------------------------


def test_every_declared_kind_has_a_consumer_that_can_see_it() -> None:
    # Arrange — the real declarations against the real CLI kind filter.
    # Act
    report = audit_jobs()
    # Assert
    assert not [f for f in report.inert if f.form is Form.CONSUMER], report.render()


def test_no_consumer_group_filters_on_an_impossible_kind() -> None:
    # Arrange — a group filtering outside ALLOWED_KINDS can never match a
    # single job, because JobSpec.validate() rejects that kind at
    # construction.
    # Act
    report = audit_jobs()
    # Assert
    assert not [f for f in report.inert if f.form is Form.IMPOSSIBLE_KIND], (
        report.render()
    )


def test_detector_fires_on_the_historical_group_name_as_kind_bug() -> None:
    # Arrange — THE RED PROOF. This is the exact mapping production used
    # for weeks: the group name passed straight through as the kind
    # filter. `sac dev systemd list` asked for kind="systemd", which is not
    # in ALLOWED_KINDS, so all four sac timers were invisible to their own
    # CLI while 13 tests stayed green.
    # Act
    findings = audit_consumers(
        declared_kinds=frozenset({"timer"}),
        group_kinds=_PRE_FIX_GROUP_KINDS,
        allowed_kinds=frozenset(jobs_mod.ALLOWED_KINDS),
    )
    inert = {f.subject for f in findings if f.verdict is Verdict.INERT}
    # Assert — both dead groups AND the orphaned declared kind are named.
    assert inert == {"sac dev systemd", "sac dev daemon", "kind=timer"}


def test_declared_kind_with_no_consumer_group_reads_inert() -> None:
    # Arrange — a legal kind that no group covers: the jobs exist and are
    # discoverable, but the CLI can neither list nor install them.
    # Act
    findings = audit_consumers(
        declared_kinds=frozenset({"service"}),
        group_kinds={"cron": frozenset({"cron"})},
        allowed_kinds=frozenset(jobs_mod.ALLOWED_KINDS),
    )
    # Assert
    assert findings[0].verdict is Verdict.INERT


def test_consumer_group_kinds_are_all_legal_kinds() -> None:
    # Arrange — pin the SSOT itself against the canonical taxonomy.
    groups = GROUP_KINDS
    # Act
    covered: set[str] = set()
    for kinds in groups.values():
        covered |= kinds
    # Assert
    assert covered <= jobs_mod.ALLOWED_KINDS


# ---------------------------------------------------------------------------
# Dataclass contract — validators fire at construction, like JobSpec
# ---------------------------------------------------------------------------


def test_finding_rejects_a_verdict_that_is_not_a_verdict() -> None:
    # Arrange — a stringly-typed verdict is how "unknown" silently becomes
    # "inert" three refactors later.
    kwargs = dict(form=Form.CONSUMER, subject="x", detail="d")

    # Act
    def _build():
        return Finding(verdict="inert", **kwargs)  # type: ignore[arg-type]

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_finding_requires_stated_evidence() -> None:
    # Arrange
    kwargs = dict(form=Form.CONSUMER, subject="x", verdict=Verdict.INERT)

    # Act
    def _build():
        return Finding(detail="", **kwargs)

    # Assert
    with pytest.raises(ValueError):
        _build()


def test_report_does_not_count_unknown_as_inert() -> None:
    # Arrange — the three-state discipline, at the report boundary.
    report = InertReport(
        findings=(
            Finding(
                form=Form.DISCOVERY,
                subject="sac.maybe",
                verdict=Verdict.UNKNOWN,
                detail="cannot tell",
            ),
        )
    )
    # Act
    inert = report.inert
    # Assert
    assert inert == ()


def test_render_names_each_dangling_pair_and_its_evidence() -> None:
    # Arrange — the output has to be readable enough to act on.
    report = InertReport(
        findings=(
            Finding(
                form=Form.CONSUMER,
                subject="kind=timer",
                verdict=Verdict.INERT,
                detail="no group filters on it",
            ),
        )
    )
    # Act
    text = report.render()
    # Assert
    assert "kind=timer" in text and "no group filters on it" in text


def test_render_is_quiet_when_nothing_dangles() -> None:
    # Arrange
    report = InertReport(findings=())
    # Act
    text = report.render()
    # Assert
    assert text == "no inert declarations found"
