"""Mutation-proof tests for the install-integrity DECISION layer.

Both directions are exercised for every reason, because a detector that
never fires and one that always fires are equally useless:

* RED — the evidence shape from a real incident MUST read BROKEN with the
  right reason.
* GREEN — the shape that merely LOOKS like it (a healthy editable install
  resolving to its own live target; a pointer we could not stat) MUST NOT.

Every reason here was neutered in place and the corresponding test
confirmed to FAIL before this file was accepted — a test that passes with
the logic removed is not a test.

The decision layer is PURE, so nothing here builds a venv: evidence is
constructed as dataclasses. The real-filesystem end of the same five
reasons lives in ``test__install_integrity_probe.py``.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003). No monkeypatch (NM002) — there is no global state to patch.
"""

from __future__ import annotations

from scitex_agent_container._maintenance import _install_integrity_model as M
from scitex_agent_container._maintenance import _install_integrity_predicate as P

_SITE = "/opt/venv-sac/lib/python3.12/site-packages"


def _pointer(
    target: str, exists: bool | None = True, path: str = ""
) -> M.EditablePointer:
    return M.EditablePointer(
        path=path or (_SITE + "/_editable_impl_pkg.pth"),
        shape=M.POINTER_PTH_PATH,
        target=target,
        target_exists=exists,
    )


def _evidence(**over) -> M.DistributionEvidence:
    """A HEALTHY wheel install, overridden per test to make one thing wrong."""
    base = dict(
        name="scitex-agent-container",
        dist_infos=(_SITE + "/scitex_agent_container-0.24.25.dist-info",),
        top_level_names=("scitex_agent_container",),
        top_level_known=True,
        code_paths=(_SITE + "/scitex_agent_container",),
        pointers=(),
        imported_path=_SITE + "/scitex_agent_container",
        import_resolution_known=True,
    )
    base.update(over)
    return M.DistributionEvidence(**base)


# ---------------------------------------------------------------------------
# Baseline — the healthy shape must read OK, or every RED below is vacuous
# ---------------------------------------------------------------------------
def test_healthy_wheel_install_reads_ok():
    # Arrange
    evidence = _evidence()
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_OK


# ---------------------------------------------------------------------------
# Reason 1 — RESOLVES_OUTSIDE_SITE_PACKAGES
# ---------------------------------------------------------------------------
def test_unexplained_outside_resolution_reads_broken():
    # Arrange — imports land in a worktree no pointer accounts for.
    evidence = _evidence(imported_path="/home/u/proj/pkg/.worktrees/scratch/src/pkg")
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_RESOLVES_OUTSIDE in verdict.reasons


def test_outside_resolution_names_the_real_path():
    # Arrange
    outside = "/home/u/proj/pkg/.worktrees/scratch/src/pkg"
    evidence = _evidence(imported_path=outside)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert — the evidence path is IN the message, not just a token.
    assert any(outside in detail for detail in verdict.details)


def test_live_editable_pointer_explains_outside_resolution():
    # Arrange — GREEN: a healthy editable install IS outside site-packages.
    target = "/home/u/proj/pkg/src"
    evidence = _evidence(
        code_paths=(),
        imported_path=target + "/pkg",
        pointers=(_pointer(target, exists=True),),
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_RESOLVES_OUTSIDE not in verdict.reasons


def test_unobservable_import_never_reads_outside():
    # Arrange — GREEN: a foreign venv; the leg was not run, so it cannot fire.
    evidence = _evidence(imported_path="", import_resolution_known=False)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_RESOLVES_OUTSIDE not in verdict.reasons


# ---------------------------------------------------------------------------
# Reason 2 — DEAD_POINTER
# ---------------------------------------------------------------------------
def test_dead_pointer_reads_broken():
    # Arrange — the August shape's pointer: target deleted with its worktree.
    evidence = _evidence(
        code_paths=(),
        pointers=(_pointer("/home/u/proj/sac/.worktrees/gone/src", exists=False),),
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_DEAD_POINTER in verdict.reasons


def test_live_pointer_is_not_dead():
    # Arrange — GREEN
    evidence = _evidence(code_paths=(), pointers=(_pointer("/home/u/proj/x/src"),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_DEAD_POINTER not in verdict.reasons


def test_unstatable_pointer_target_is_not_dead():
    # Arrange — GREEN: "could not stat" must never fabricate a dead pointer.
    evidence = _evidence(code_paths=(), pointers=(_pointer("/mnt/gone", exists=None),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_DEAD_POINTER not in verdict.reasons


def test_unstatable_pointer_target_reads_unknown():
    # Arrange — and it must not read OK either.
    evidence = _evidence(code_paths=(), pointers=(_pointer("/mnt/gone", exists=None),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Reason 3 — SHADOWED_POINTER (the 2026-08-09 case)
# ---------------------------------------------------------------------------
def test_pointer_beside_real_package_reads_shadowed():
    # Arrange — pointer AND a real package dir: imports work, pointer inert.
    evidence = _evidence(pointers=(_pointer("/home/u/proj/sac/src"),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_SHADOWED_POINTER in verdict.reasons


def test_pointer_without_real_package_is_not_shadowed():
    # Arrange — GREEN: a plain editable install has no copy to shadow it.
    evidence = _evidence(code_paths=(), pointers=(_pointer("/home/u/proj/sac/src"),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_SHADOWED_POINTER not in verdict.reasons


def test_shadowing_alone_reads_broken():
    # Arrange — shadowing WITHOUT a dead pointer: `pip install -e .` then
    # `pip install .` on top. The target is alive, imports resolve INSIDE
    # site-packages, nothing is missing — and the pointer is still inert,
    # so propagation is silently dead. Isolated from every other reason on
    # purpose: shadowing has to stand on its own, or the August case is
    # only ever caught by accident through its dead pointer.
    evidence = _evidence(pointers=(_pointer("/home/u/proj/sac/src", exists=True),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_BROKEN


def test_shadowing_alone_names_only_that_reason():
    # Arrange — same isolated shape; nothing else may fire.
    evidence = _evidence(pointers=(_pointer("/home/u/proj/sac/src", exists=True),))
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.reasons == (M.REASON_SHADOWED_POINTER,)


def test_august_shape_reports_both_shadowed_and_dead():
    # Arrange — one verdict must carry BOTH findings; either alone hides half
    # the repair.
    evidence = _evidence(
        pointers=(_pointer("/home/u/proj/sac/.worktrees/gone/src", exists=False),)
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert set(verdict.reasons) == {
        M.REASON_DEAD_POINTER,
        M.REASON_SHADOWED_POINTER,
    }


# ---------------------------------------------------------------------------
# Reason 4 — ORPHANED_DIST_INFO (the 2026-07-16 case)
# ---------------------------------------------------------------------------
def test_dist_info_without_code_reads_orphaned():
    # Arrange — dist-info advertising a version whose code is gone.
    evidence = _evidence(code_paths=(), imported_path="")
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_ORPHANED_DIST_INFO in verdict.reasons


def test_live_pointer_prevents_orphan_verdict():
    # Arrange — GREEN: an editable install legitimately keeps code elsewhere.
    evidence = _evidence(
        code_paths=(),
        imported_path="",
        pointers=(_pointer("/home/u/proj/x/src"),),
        declared_editable=True,
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_ORPHANED_DIST_INFO not in verdict.reasons


def test_undeterminable_ownership_prevents_orphan_verdict():
    # Arrange — GREEN: no top_level.txt and no RECORD means we cannot say
    # what code SHOULD be there, so "no code behind it" is not decidable.
    evidence = _evidence(code_paths=(), imported_path="", top_level_known=False)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_ORPHANED_DIST_INFO not in verdict.reasons


def test_undeterminable_ownership_reads_unknown():
    # Arrange — and it must not read OK either.
    evidence = _evidence(code_paths=(), imported_path="", top_level_known=False)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Reason 5 — DUPLICATE_DIST_INFO
# ---------------------------------------------------------------------------
def test_two_dist_infos_read_duplicate():
    # Arrange — the measured scitex_cards shape: 0.17.5 beside 0.17.7.
    evidence = _evidence(
        dist_infos=(
            _SITE + "/scitex_cards-0.17.5.dist-info",
            _SITE + "/scitex_cards-0.17.7.dist-info",
        )
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_DUPLICATE_DIST_INFO in verdict.reasons


def test_single_dist_info_is_not_duplicate():
    # Arrange — GREEN
    evidence = _evidence()
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.REASON_DUPLICATE_DIST_INFO not in verdict.reasons


def test_duplicate_detail_lists_both_dist_infos():
    # Arrange
    evidence = _evidence(
        dist_infos=(
            _SITE + "/scitex_cards-0.17.5.dist-info",
            _SITE + "/scitex_cards-0.17.7.dist-info",
        )
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert — both fossils named, so the operator knows which to remove.
    assert all(version in " ".join(verdict.details) for version in ("0.17.5", "0.17.7"))


# ---------------------------------------------------------------------------
# UNKNOWN never collapses into either pole
# ---------------------------------------------------------------------------
def test_absent_distribution_reads_unknown():
    # Arrange
    evidence = M.DistributionEvidence(name="not-installed", absent=True)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_UNKNOWN


def test_absent_distribution_is_never_broken():
    # Arrange
    evidence = M.DistributionEvidence(name="not-installed", absent=True)
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.reasons == ()


def test_unreadable_site_reads_unknown_report():
    # Arrange
    site = M.SiteEvidence(
        site_packages="/opt/venv-sac/lib/python3.12/site-packages",
        readable=False,
        read_error="PermissionError: [Errno 13]",
    )
    # Act
    report = P.build_report(site)
    # Assert
    assert report.site_unknown


def test_unreadable_site_yields_no_verdicts():
    # Arrange
    site = M.SiteEvidence(site_packages=_SITE, readable=False, read_error="boom")
    # Act
    report = P.build_report(site)
    # Assert — nothing was observed, so nothing may be claimed.
    assert report.verdicts == ()


def test_broken_distribution_still_carries_unknown_legs():
    # Arrange — a definite finding must not erase the gap beside it.
    evidence = _evidence(
        dist_infos=(_SITE + "/a-1.dist-info", _SITE + "/a-2.dist-info"),
        pointers=(_pointer("/mnt/gone", exists=None),),
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert M.UNKNOWN_TARGET_UNSTATABLE in verdict.unknown_reasons


def test_broken_outranks_unknown_in_state():
    # Arrange — same evidence: the STATE is the actionable one.
    evidence = _evidence(
        dist_infos=(_SITE + "/a-1.dist-info", _SITE + "/a-2.dist-info"),
        pointers=(_pointer("/mnt/gone", exists=None),),
    )
    # Act
    verdict = P.classify_distribution(evidence, _SITE)
    # Assert
    assert verdict.state == M.STATE_BROKEN


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
def _report_with(evidence: M.DistributionEvidence) -> M.InstallIntegrityReport:
    return P.build_report(
        M.SiteEvidence(site_packages=_SITE, readable=True, distributions=(evidence,))
    )


def test_exit_code_is_one_when_broken():
    # Arrange
    report = _report_with(_evidence(code_paths=(), imported_path=""))
    # Act
    code = P.exit_code_for(report)
    # Assert
    assert code == 1


def test_exit_code_is_zero_when_only_unknown():
    # Arrange — UNKNOWN alone is an absence of evidence, not a failure.
    report = _report_with(M.DistributionEvidence(name="ghost", absent=True))
    # Act
    code = P.exit_code_for(report)
    # Assert
    assert code == 0


def test_strict_exit_code_is_two_when_only_unknown():
    # Arrange
    report = _report_with(M.DistributionEvidence(name="ghost", absent=True))
    # Act
    code = P.exit_code_for(report, strict=True)
    # Assert
    assert code == 2


def test_exit_code_is_zero_when_clean():
    # Arrange
    report = _report_with(_evidence())
    # Act
    code = P.exit_code_for(report)
    # Assert
    assert code == 0


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def test_reason_breakdown_counts_by_reason():
    # Arrange — two distributions, both duplicated.
    dup = dict(dist_infos=(_SITE + "/a-1.dist-info", _SITE + "/a-2.dist-info"))
    report = P.build_report(
        M.SiteEvidence(
            site_packages=_SITE,
            readable=True,
            distributions=(
                _evidence(name="a", **dup),
                _evidence(name="b", **dup),
            ),
        )
    )
    # Act
    breakdown = report.reason_breakdown()
    # Assert
    assert breakdown[M.REASON_DUPLICATE_DIST_INFO] == 2


def test_sibling_prefix_is_not_inside_directory():
    # Arrange — /a/bc must never count as living under /a/b, or every
    # containment answer above it is unsound.
    child, parent = "/a/bc", "/a/b"
    # Act
    inside = P.is_under(child, parent)
    # Assert
    assert not inside


def test_child_path_is_inside_directory():
    # Arrange
    child, parent = "/a/b/c", "/a/b"
    # Act
    inside = P.is_under(child, parent)
    # Assert
    assert inside
