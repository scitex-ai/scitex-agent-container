"""Tests for the install-integrity data types (the serialisation contract).

The reason strings and the ``to_dict`` shapes here are API: they land in
``sac installation check --json``, which is what a cron line and a repair
step read. A silently renamed reason token, or a dropped key, breaks
consumers that never see a traceback — so the vocabulary is pinned here
rather than left to the renderer to imply.

The DECISIONS that produce these values are tested in
``test__install_integrity_predicate.py``; this file only pins the shapes.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003). No monkeypatch (NM002).
"""

from __future__ import annotations

from scitex_agent_container._maintenance import _install_integrity_model as M

_SITE = "/opt/venv-sac/lib/python3.12/site-packages"


def _pointer(**over) -> M.EditablePointer:
    base = dict(
        path=_SITE + "/_editable_impl_pkg.pth",
        shape=M.POINTER_PTH_PATH,
        target="/home/u/proj/pkg/src",
        target_exists=True,
    )
    base.update(over)
    return M.EditablePointer(**base)


# ---------------------------------------------------------------------------
# Pointer liveness is tri-state, and neither pole absorbs the third
# ---------------------------------------------------------------------------
def test_pointer_with_present_target_is_live():
    # Arrange
    pointer = _pointer(target_exists=True)
    # Act
    live = pointer.live
    # Assert
    assert live


def test_pointer_with_missing_target_is_dead():
    # Arrange
    pointer = _pointer(target_exists=False)
    # Act
    dead = pointer.dead
    # Assert
    assert dead


def test_unstatable_pointer_is_not_dead():
    # Arrange — "could not stat" must never read as "not there".
    pointer = _pointer(target_exists=None)
    # Act
    dead = pointer.dead
    # Assert
    assert not dead


def test_unstatable_pointer_is_not_live():
    # Arrange — nor as "there".
    pointer = _pointer(target_exists=None)
    # Act
    live = pointer.live
    # Assert
    assert not live


def test_unparsable_pointer_is_not_live():
    # Arrange — no target parsed means nothing was proven either way.
    pointer = _pointer(target="", target_exists=True)
    # Act
    live = pointer.live
    # Assert
    assert not live


# ---------------------------------------------------------------------------
# Report partitioning
# ---------------------------------------------------------------------------
def _report(*states: str) -> M.InstallIntegrityReport:
    return M.InstallIntegrityReport(
        site_packages=_SITE,
        verdicts=tuple(
            M.DistributionVerdict(name="d" + str(i), state=state)
            for i, state in enumerate(states)
        ),
    )


def test_report_partitions_broken_rows():
    # Arrange
    report = _report(M.STATE_OK, M.STATE_BROKEN, M.STATE_UNKNOWN)
    # Act
    broken = report.broken
    # Assert
    assert len(broken) == 1


def test_report_partitions_unknown_rows():
    # Arrange
    report = _report(M.STATE_OK, M.STATE_BROKEN, M.STATE_UNKNOWN)
    # Act
    unknown = report.unknown
    # Assert
    assert len(unknown) == 1


def test_unknown_rows_are_not_counted_as_ok():
    # Arrange — the whole doctrine in one assertion.
    report = _report(M.STATE_OK, M.STATE_UNKNOWN, M.STATE_UNKNOWN)
    # Act
    ok = report.ok
    # Assert
    assert len(ok) == 1


def test_summary_names_unobservable_imports():
    # Arrange — a foreign venv must say the leg was not run.
    report = M.InstallIntegrityReport(
        site_packages=_SITE, import_resolution=M.IMPORTS_UNAVAILABLE
    )
    # Act
    line = report.summary_line()
    # Assert
    assert "UNOBSERVABLE" in line


def test_summary_omits_the_note_when_imports_are_live():
    # Arrange
    report = M.InstallIntegrityReport(
        site_packages=_SITE, import_resolution=M.IMPORTS_LIVE
    )
    # Act
    line = report.summary_line()
    # Assert
    assert "UNOBSERVABLE" not in line


def test_reason_breakdown_follows_declared_order():
    # Arrange — deterministic output, never set-iteration order.
    report = M.InstallIntegrityReport(
        site_packages=_SITE,
        verdicts=(
            M.DistributionVerdict(
                name="a",
                state=M.STATE_BROKEN,
                reasons=(M.REASON_DUPLICATE_DIST_INFO, M.REASON_DEAD_POINTER),
            ),
        ),
    )
    # Act
    keys = list(report.reason_breakdown())
    # Assert
    assert keys == [M.REASON_DEAD_POINTER, M.REASON_DUPLICATE_DIST_INFO]


# ---------------------------------------------------------------------------
# JSON contract
# ---------------------------------------------------------------------------
def test_report_dict_carries_the_counts_block():
    # Arrange
    report = _report(M.STATE_OK, M.STATE_BROKEN, M.STATE_UNKNOWN)
    # Act
    payload = report.to_dict()
    # Assert
    assert payload["counts"] == {"total": 3, "ok": 1, "broken": 1, "unknown": 1}


def test_report_dict_names_the_import_resolution():
    # Arrange — a consumer must be able to tell a skipped leg from a clean one.
    report = M.InstallIntegrityReport(
        site_packages=_SITE, import_resolution=M.IMPORTS_UNAVAILABLE
    )
    # Act
    payload = report.to_dict()
    # Assert
    assert payload["import_resolution"] == M.IMPORTS_UNAVAILABLE


def test_verdict_dict_carries_its_evidence():
    # Arrange
    verdict = M.DistributionVerdict(
        name="pkg",
        state=M.STATE_BROKEN,
        reasons=(M.REASON_DEAD_POINTER,),
        evidence=M.DistributionEvidence(name="pkg", pointers=(_pointer(),)),
    )
    # Act
    payload = verdict.to_dict()
    # Assert — the pointer path is the repair instruction; it must survive.
    assert payload["evidence"]["pointers"][0]["path"].endswith("_editable_impl_pkg.pth")


def test_verdict_dict_without_evidence_is_null():
    # Arrange
    verdict = M.DistributionVerdict(name="pkg", state=M.STATE_OK)
    # Act
    payload = verdict.to_dict()
    # Assert
    assert payload["evidence"] is None


def test_every_reason_appears_in_the_declared_order():
    # Arrange — a new reason that forgets REASON_ORDER would vanish from
    # every breakdown without failing anything else.
    declared = {
        M.REASON_RESOLVES_OUTSIDE,
        M.REASON_DEAD_POINTER,
        M.REASON_SHADOWED_POINTER,
        M.REASON_ORPHANED_DIST_INFO,
        M.REASON_DUPLICATE_DIST_INFO,
    }
    # Act
    ordered = set(M.REASON_ORDER)
    # Assert
    assert ordered == declared


def test_canonical_name_folds_underscores_and_case():
    # Arrange
    raw = "SciTeX_Agent_Container"
    # Act
    canonical = M.canonical_dist_name(raw)
    # Assert
    assert canonical == "scitex-agent-container"
