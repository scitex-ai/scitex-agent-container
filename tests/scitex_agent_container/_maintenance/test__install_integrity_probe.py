"""End-to-end tests for the install-integrity PROBE, on real directories.

No mocks and no monkeypatch: every test builds a REAL venv layout under
``tmp_path`` — ``venv/lib/python3.12/site-packages`` with real
``*.dist-info`` dirs, real ``RECORD``/``top_level.txt`` files, real
``.pth`` pointers and real package directories. That is the point: the
predicate's unit tests prove the DECISIONS, and these prove the probe
turns actual on-disk shapes into the right evidence. A parser bug lives
exactly in the gap between those two.

Each venv is inspected as a FOREIGN venv (not this interpreter's), so the
import-resolution leg is honestly reported as unobservable rather than
answered from the test runner's own ``sys.path``.

The operator's real ``/opt/venv-sac`` is never touched — this whole module
is read-only against ``tmp_path``.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._maintenance import _install_integrity_model as M
from scitex_agent_container._maintenance import _install_integrity_probe as PR

_PY = "python3.12"


@pytest.fixture
def venv(tmp_path):
    """A real, empty venv layout. Yield-fixture: no monkeypatch (NM002)."""
    root = tmp_path / "venv"
    (root / "lib" / _PY / "site-packages").mkdir(parents=True)
    yield root


def _site(venv_root: Path) -> Path:
    return venv_root / "lib" / _PY / "site-packages"


def _dist_info(
    venv_root: Path,
    name: str,
    version: str,
    *,
    top_level: str | None = None,
    record_rows: list[str] | None = None,
) -> Path:
    """A REAL dist-info: METADATA + RECORD, optional top_level.txt."""
    dist_info = _site(venv_root) / (name + "-" + version + ".dist-info")
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Name: " + name + "\nVersion: " + version + "\n"
    )
    rows = record_rows if record_rows is not None else [name + "/__init__.py,,"]
    rows = rows + [dist_info.name + "/METADATA,,", dist_info.name + "/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")
    if top_level is not None:
        (dist_info / "top_level.txt").write_text(top_level + "\n")
    return dist_info


def _package(venv_root: Path, name: str) -> Path:
    """A REAL importable package directory inside site-packages."""
    pkg = _site(venv_root) / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def _pth(venv_root: Path, filename: str, body: str) -> Path:
    path = _site(venv_root) / filename
    path.write_text(body)
    return path


def _verdict_for(venv_root: Path, name: str) -> M.DistributionVerdict:
    report = PR.inspect_install(venv_root)
    return next(v for v in report.verdicts if v.name == name)


# ---------------------------------------------------------------------------
# Baseline — a healthy wheel install on real disk
# ---------------------------------------------------------------------------
def test_healthy_wheel_install_reads_ok(venv):
    # Arrange
    _dist_info(venv, "scitex_cards", "0.32.3", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    # Act
    verdict = _verdict_for(venv, "scitex-cards")
    # Assert
    assert verdict.state == M.STATE_OK


def test_foreign_venv_reports_imports_unobservable(venv):
    # Arrange
    _dist_info(venv, "scitex_cards", "0.32.3", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    # Act
    report = PR.inspect_install(venv)
    # Assert — never silently answered from the test runner's own sys.path.
    assert report.import_resolution == M.IMPORTS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Reason 2 — DEAD_POINTER, through each emitted shape
# ---------------------------------------------------------------------------
def test_uv_pth_with_missing_target_reads_dead(venv, tmp_path):
    # Arrange — the /opt/venv-sac shape: a bare path to a deleted worktree.
    _dist_info(venv, "scitex_agent_container", "0.24.25", top_level="x_not_here")
    _pth(venv, "_editable_impl_scitex_agent_container.pth", str(tmp_path / "gone/src"))
    # Act
    verdict = _verdict_for(venv, "scitex-agent-container")
    # Assert
    assert M.REASON_DEAD_POINTER in verdict.reasons


def test_setuptools_finder_with_missing_target_reads_dead(venv, tmp_path):
    # Arrange — the strict-mode pair: .pth naming a finder, finder holding
    # a MAPPING to a directory that is gone.
    _dist_info(venv, "scitex_dev", "0.31.0", top_level="x_not_here")
    _pth(
        venv,
        "__editable__.scitex_dev-0.31.0.pth",
        "import __editable___scitex_dev_0_31_0_finder; "
        "__editable___scitex_dev_0_31_0_finder.install()\n",
    )
    _pth(
        venv,
        "__editable___scitex_dev_0_31_0_finder.py",
        "MAPPING = {'scitex_dev': '" + str(tmp_path / "gone" / "scitex_dev") + "'}\n",
    )
    # Act
    verdict = _verdict_for(venv, "scitex-dev")
    # Assert
    assert M.REASON_DEAD_POINTER in verdict.reasons


def test_pth_with_live_target_is_not_dead(venv, tmp_path):
    # Arrange — GREEN: a healthy editable install.
    live = tmp_path / "checkout" / "src"
    (live / "scitex_dev").mkdir(parents=True)
    _dist_info(venv, "scitex_dev", "0.31.0", top_level="scitex_dev")
    _pth(venv, "__editable__.scitex_dev-0.31.0.pth", str(live))
    # Act
    verdict = _verdict_for(venv, "scitex-dev")
    # Assert
    assert M.REASON_DEAD_POINTER not in verdict.reasons


def test_coverage_pth_never_becomes_a_pointer(venv):
    # Arrange — GREEN: the bootstrap .pth every venv carries.
    _dist_info(venv, "scitex_cards", "0.32.3", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    _pth(
        venv,
        "a1_coverage.pth",
        "import os\nif os.environ.get('COVERAGE_PROCESS_START'):\n    pass\n",
    )
    # Act
    report = PR.inspect_install(venv)
    # Assert
    assert report.broken == ()


# ---------------------------------------------------------------------------
# Reason 3 — SHADOWED_POINTER (the 2026-08-09 /opt/venv-sac shape)
# ---------------------------------------------------------------------------
def test_pointer_beside_real_package_reads_shadowed(venv, tmp_path):
    # Arrange — the measured August state, reproduced on disk.
    _dist_info(
        venv, "scitex_agent_container", "0.24.25", top_level="scitex_agent_container"
    )
    _package(venv, "scitex_agent_container")
    _pth(venv, "_editable_impl_scitex_agent_container.pth", str(tmp_path / "gone/src"))
    # Act
    verdict = _verdict_for(venv, "scitex-agent-container")
    # Assert
    assert M.REASON_SHADOWED_POINTER in verdict.reasons


def test_august_shape_reports_both_shadowed_and_dead(venv, tmp_path):
    # Arrange — the measured /opt/venv-sac state end to end. Imports would
    # resolve INSIDE site-packages here, which is exactly what makes the
    # naive __file__ predicate call it clean. BOTH findings must surface;
    # either alone hides half the repair.
    _dist_info(
        venv, "scitex_agent_container", "0.24.25", top_level="scitex_agent_container"
    )
    _package(venv, "scitex_agent_container")
    _pth(venv, "_editable_impl_scitex_agent_container.pth", str(tmp_path / "gone/src"))
    # Act
    verdict = _verdict_for(venv, "scitex-agent-container")
    # Assert
    assert set(verdict.reasons) == {
        M.REASON_DEAD_POINTER,
        M.REASON_SHADOWED_POINTER,
    }


def test_shadowing_alone_reads_broken_on_real_disk(venv, tmp_path):
    # Arrange — shadowing WITHOUT a dead pointer: `pip install -e .` then
    # `pip install .` over it. The target is alive and nothing is missing;
    # the pointer is merely inert. Isolated so this reason must stand on
    # its own rather than riding the dead-pointer finding.
    live = tmp_path / "checkout" / "src"
    (live / "scitex_agent_container").mkdir(parents=True)
    _dist_info(
        venv, "scitex_agent_container", "0.24.25", top_level="scitex_agent_container"
    )
    _package(venv, "scitex_agent_container")
    _pth(venv, "_editable_impl_scitex_agent_container.pth", str(live))
    # Act
    verdict = _verdict_for(venv, "scitex-agent-container")
    # Assert
    assert verdict.reasons == (M.REASON_SHADOWED_POINTER,)


def test_editable_install_without_copy_is_not_shadowed(venv, tmp_path):
    # Arrange — GREEN
    live = tmp_path / "checkout" / "src"
    (live / "scitex_dev").mkdir(parents=True)
    _dist_info(venv, "scitex_dev", "0.31.0", top_level="scitex_dev")
    _pth(venv, "__editable__.scitex_dev-0.31.0.pth", str(live))
    # Act
    verdict = _verdict_for(venv, "scitex-dev")
    # Assert
    assert M.REASON_SHADOWED_POINTER not in verdict.reasons


# ---------------------------------------------------------------------------
# Reason 4 — ORPHANED_DIST_INFO (the 2026-07-16 shape)
# ---------------------------------------------------------------------------
def test_dist_info_with_no_code_reads_orphaned(venv):
    # Arrange — dist-info claiming a module that is not there.
    _dist_info(venv, "scitex_dev", "0.31.0", top_level="scitex_dev")
    # Act
    verdict = _verdict_for(venv, "scitex-dev")
    # Assert
    assert M.REASON_ORPHANED_DIST_INFO in verdict.reasons


def test_record_supplies_ownership_without_top_level(venv):
    # Arrange — modern wheels ship no top_level.txt; RECORD must answer, or
    # every one of them would read as an orphan.
    _dist_info(venv, "gitpython", "3.1.50", record_rows=["git/__init__.py,,"])
    _package(venv, "git")
    # Act
    verdict = _verdict_for(venv, "gitpython")
    # Assert
    assert verdict.state == M.STATE_OK


def test_metadata_only_dist_reads_unknown_not_orphaned(venv):
    # Arrange — a real meta-package (measured: fastmcp 3.4.6) lists no
    # modules at all, so "has code behind it" is undeterminable.
    _dist_info(venv, "fastmcp", "3.4.6", record_rows=[])
    # Act
    verdict = _verdict_for(venv, "fastmcp")
    # Assert
    assert verdict.state == M.STATE_UNKNOWN


def test_interrupted_pip_debris_reads_orphaned(venv):
    # Arrange — pip renames a dist's first char to `~` while replacing it
    # and leaves the dir behind if interrupted. Measured live in
    # /opt/venv-sac: `~citex_agent_container-0.21.13.dist-info`.
    _dist_info(
        venv,
        "~citex_agent_container",
        "0.21.13",
        record_rows=["scitex_agent_container/__init__.py,,"],
    )
    _package(venv, "scitex_agent_container")
    # Act
    verdict = _verdict_for(venv, "~citex-agent-container")
    # Assert — it must NOT be credited with the real dist's code.
    assert M.REASON_ORPHANED_DIST_INFO in verdict.reasons


# ---------------------------------------------------------------------------
# Reason 5 — DUPLICATE_DIST_INFO
# ---------------------------------------------------------------------------
def test_two_dist_infos_read_duplicate(venv):
    # Arrange — the measured scitex_cards shape: 0.17.5 beside 0.17.7.
    _dist_info(venv, "scitex_cards", "0.17.5", top_level="scitex_cards")
    _dist_info(venv, "scitex_cards", "0.17.7", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    # Act
    verdict = _verdict_for(venv, "scitex-cards")
    # Assert
    assert M.REASON_DUPLICATE_DIST_INFO in verdict.reasons


def test_duplicate_verdict_lists_both_dist_infos(venv):
    # Arrange
    _dist_info(venv, "scitex_cards", "0.17.5", top_level="scitex_cards")
    _dist_info(venv, "scitex_cards", "0.17.7", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    # Act
    verdict = _verdict_for(venv, "scitex-cards")
    # Assert
    assert len(verdict.evidence.dist_infos) == 2


# ---------------------------------------------------------------------------
# UNKNOWN paths through the probe
# ---------------------------------------------------------------------------
def test_missing_site_packages_reads_site_unknown(tmp_path):
    # Arrange — a venv path that does not exist at all.
    # Act
    report = PR.inspect_install(tmp_path / "no-such-venv")
    # Assert
    assert report.site_unknown


def test_missing_site_packages_produces_no_verdicts(tmp_path):
    # Arrange
    # Act
    report = PR.inspect_install(tmp_path / "no-such-venv")
    # Assert — nothing observed, so nothing claimed.
    assert report.verdicts == ()


def test_requested_absent_dist_reads_unknown(venv):
    # Arrange
    _dist_info(venv, "scitex_cards", "0.32.3", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    # Act
    report = PR.inspect_install(venv, dists=("not-installed",))
    # Assert
    assert report.unknown[0].unknown_reasons == (M.UNKNOWN_DIST_ABSENT,)


def test_dist_filter_narrows_the_report(venv):
    # Arrange
    _dist_info(venv, "scitex_cards", "0.32.3", top_level="scitex_cards")
    _package(venv, "scitex_cards")
    _dist_info(venv, "scitex_dev", "0.43.0", top_level="scitex_dev")
    _package(venv, "scitex_dev")
    # Act
    report = PR.inspect_install(venv, dists=("scitex-cards",))
    # Assert
    assert [v.name for v in report.verdicts] == ["scitex-cards"]


# ---------------------------------------------------------------------------
# site-packages resolution
# ---------------------------------------------------------------------------
def test_site_packages_path_is_accepted_directly(venv):
    # Arrange
    site = _site(venv)
    # Act
    resolved, _note = PR.resolve_site_packages(site)
    # Assert
    assert resolved == str(site)


def test_venv_root_resolves_to_its_site_packages(venv):
    # Arrange
    # Act
    resolved, _note = PR.resolve_site_packages(venv)
    # Assert
    assert resolved == str(_site(venv))


def test_two_python_dirs_are_not_merged(venv):
    # Arrange — merging would invent duplicate dist-infos that exist for
    # neither interpreter.
    (venv / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    # Act
    _resolved, note = PR.resolve_site_packages(venv)
    # Assert
    assert "not merged" in note


def test_default_target_is_this_interpreters_venv():
    # Arrange
    # Act
    resolved, _note = PR.resolve_site_packages(None)
    # Assert
    assert resolved == PR.running_site_packages()
