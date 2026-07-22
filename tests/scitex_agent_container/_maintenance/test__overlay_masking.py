"""Mutation-proof tests for the overlay-masking detector (2026-07-22 incident).

Both directions are exercised, because a detector that never fires and one
that always fires are equally useless:

* RED — a planted stale ``scitex_cards`` dist-info in a temp overlay upper,
  for a package the base provides, MUST read MASKED.
* GREEN — the benign overlayfs copy-up shape (package dir with only
  ``__pycache__``, zero top-level ``*.py``, no dist-info) MUST read CLEAN.
  Counting these as masking is exactly the false scare the incident sweep
  already produced once.

No mocks: real temp directory layouts mirroring
``overlays/<agent>/upper/opt/venv-sac/lib/python3.12/site-packages``; the
base-set seam is injected data (the ``BasePackageSet`` value object), and
the apptainer path runs a REAL fake binary on PATH (same pattern as
``_drift/test_versions.py``). Everything lives under ``tmp_path`` — the
operator's real ``~/.scitex/agent-container/`` tree is never touched.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._maintenance import _overlay_masking as OM
from scitex_agent_container._maintenance import _overlay_masking_model as M

# The base venv's truth, as a COMPLETE set (full pip list): scitex-cards is
# base-baked at 0.17.5 — the incident package.
_BASE = M.BasePackageSet(
    packages={"scitex-cards": "0.17.5", "click": "8.1.7"},
    complete=True,
    source="test-live",
)

# The same truth read PARTIALLY (baked manifest covers scitex-* only).
_BASE_PARTIAL = M.BasePackageSet(
    packages={"scitex-cards": "0.17.5"},
    complete=False,
    source="test-manifest",
)


def _overlay(tmp_path: Path, agent: str = "agent-x") -> Path:
    """A temp overlay mirroring the fleet layout, upper venv provisioned."""
    root = tmp_path / "overlays" / agent
    (root / "upper" / "opt/venv-sac/lib/python3.12/site-packages").mkdir(parents=True)
    (root / "work").mkdir(parents=True)
    return root


def _site(root: Path) -> Path:
    return root / "upper" / "opt/venv-sac/lib/python3.12/site-packages"


def _plant_dist_info(root: Path, name: str, version: str) -> Path:
    """A REAL pip-install fossil: ``<Name>-<Version>.dist-info`` + pkg dir."""
    site = _site(root)
    dist_info = site / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    (dist_info / "RECORD").write_text("")
    pkg = site / name
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return dist_info


def _plant_benign_copyup(root: Path, name: str) -> Path:
    """The benign overlayfs copy-up: only ``__pycache__``, no ``*.py``."""
    pycache = _site(root) / name / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "__init__.cpython-312.pyc").write_bytes(b"\x00fake-pyc")
    return pycache.parent


def _config_for(root: Path, **apptainer_extra) -> SimpleNamespace:
    ap = SimpleNamespace(
        overlay=str(root), raw_args=[], image="", overlay_size="", **apptainer_extra
    )
    return SimpleNamespace(apptainer=ap, workdir=str(root.parent), name="agent-x")


# ---------------------------------------------------------------------------
# RED direction — the planted fossil MUST fire
# ---------------------------------------------------------------------------
def test_stale_distinfo_for_base_package_reads_masked(tmp_path):
    # Arrange — the incident shape: scitex_cards 0.16.1 fossil in the upper.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert verdict.verdict == M.VERDICT_MASKED


def test_masked_shadow_row_carries_fossil_and_base_versions(tmp_path):
    # Arrange
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    # Act
    shadow = OM.inspect_overlay("agent-x", root, _BASE).shadows[0]
    # Assert — canonicalised name, fossil version, base version, evidence path.
    assert (
        shadow.package,
        shadow.version,
        shadow.base_version,
        shadow.status,
    ) == ("scitex-cards", "0.16.1", "0.17.5", M.SHADOW_MASKED)


def test_same_version_distinfo_is_still_masked(tmp_path):
    # Arrange — the rule has no version qualifier: a same-version install
    # still shadows every FUTURE base rebuild.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.17.5")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert verdict.verdict == M.VERDICT_MASKED


def test_masked_detail_names_the_shadowed_package(tmp_path):
    # Arrange
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert "scitex-cards 0.16.1 (base 0.17.5)" in verdict.detail


# ---------------------------------------------------------------------------
# GREEN direction — the benign copy-up MUST NOT fire
# ---------------------------------------------------------------------------
def test_pycache_only_copyup_reads_clean(tmp_path):
    # Arrange — the false-scare shape: bare pkg dir, only __pycache__,
    # zero top-level *.py, NO dist-info.
    root = _overlay(tmp_path)
    _plant_benign_copyup(root, "scitex_cards")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert verdict.verdict == M.VERDICT_CLEAN


def test_pycache_only_copyup_is_listed_as_evidence(tmp_path):
    # Arrange
    root = _overlay(tmp_path)
    _plant_benign_copyup(root, "scitex_cards")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert — visible in the report, absent from the alarm.
    assert verdict.copyups == ("scitex_cards",)


def test_untouched_upper_venv_reads_clean(tmp_path):
    # Arrange — the fleet-normal state: overlay exists, upper has no venv.
    root = tmp_path / "overlays" / "agent-x"
    (root / "upper").mkdir(parents=True)
    (root / "work").mkdir(parents=True)
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_CLEAN,
        M.REASON_UPPER_VENV_UNTOUCHED,
    )


def test_no_declared_overlay_reads_clean(tmp_path):
    # Arrange — no overlay at all: nothing can mask the base.
    overlay_root = None
    # Act
    verdict = OM.inspect_overlay("agent-x", overlay_root, _BASE)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_CLEAN,
        M.REASON_NO_OVERLAY,
    )


def test_overlay_only_install_with_complete_base_reads_clean(tmp_path):
    # Arrange — an add the base does not provide: the legitimate use of a
    # writable overlay, not masking.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "requests", "2.31.0")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert (verdict.verdict, verdict.shadows[0].status) == (
        M.VERDICT_CLEAN,
        M.SHADOW_OVERLAY_ONLY,
    )


# ---------------------------------------------------------------------------
# UNKNOWN direction — "could not tell" must never read clean
# ---------------------------------------------------------------------------
def test_missing_overlay_root_reads_unknown_not_clean(tmp_path):
    # Arrange — declared, but nothing on disk: could be "never provisioned"
    # or "wrong host / wrong HOME"; we cannot tell which.
    root = tmp_path / "overlays" / "agent-x"
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_UNKNOWN,
        M.REASON_OVERLAY_MISSING,
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores 0o000 modes")
def test_unreadable_site_packages_reads_unknown(tmp_path):
    # Arrange — a real permission wall, not a mock.
    root = _overlay(tmp_path)
    site = _site(root)
    site.chmod(0o000)
    try:
        # Act
        verdict = OM.inspect_overlay("agent-x", root, _BASE)
    finally:
        site.chmod(0o755)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_UNKNOWN,
        M.REASON_UPPER_UNREADABLE,
    )


def test_shadow_with_unreadable_base_reads_unknown(tmp_path):
    # Arrange — a dist-info in the upper, but the base set could not be
    # read: masking can not be ruled out, so this must NOT be clean.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, None)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_UNKNOWN,
        M.REASON_BASE_UNKNOWN,
    )


def test_partial_base_set_miss_reads_unknown(tmp_path):
    # Arrange — the baked manifest covers scitex-* only; a numpy dist-info
    # missing from it proves NOTHING about what the base provides.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "numpy", "2.0.0")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE_PARTIAL)
    # Assert
    assert verdict.verdict == M.VERDICT_UNKNOWN


def test_partial_base_set_hit_still_reads_masked(tmp_path):
    # Arrange — membership in a partial set IS proof: manifest says the
    # base bakes scitex-cards, and the upper shadows it.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.0")
    # Act
    verdict = OM.inspect_overlay("agent-x", root, _BASE_PARTIAL)
    # Assert
    assert verdict.verdict == M.VERDICT_MASKED


def test_image_overlay_reads_unknown(tmp_path):
    # Arrange — a loopback image overlay is not host-readable.
    img = tmp_path / "agent-x.overlay.img"
    img.write_bytes(b"\x00ext3")
    # Act
    verdict = OM.inspect_overlay("agent-x", img, _BASE)
    # Assert
    assert (verdict.verdict, verdict.reason) == (
        M.VERDICT_UNKNOWN,
        M.REASON_IMAGE_OVERLAY,
    )


# ---------------------------------------------------------------------------
# laziness — a clean agent must never pay for a base read
# ---------------------------------------------------------------------------
def test_base_provider_not_consulted_when_upper_is_clean(tmp_path):
    # Arrange
    root = _overlay(tmp_path)
    calls: list[int] = []

    def provider() -> M.BasePackageSet:
        calls.append(1)
        return _BASE

    # Act
    OM.inspect_overlay("agent-x", root, provider)
    # Assert — no dist-info found, so the (expensive) base read never ran.
    assert calls == []


# ---------------------------------------------------------------------------
# per-agent entry: spec-driven overlay resolution
# ---------------------------------------------------------------------------
def test_agent_inspect_resolves_modeled_overlay_field(tmp_path):
    # Arrange
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    config = _config_for(root)
    # Act
    verdict = OM.inspect_agent_overlay("agent-x", config, lambda: _BASE)
    # Assert
    assert verdict.verdict == M.VERDICT_MASKED


def test_agent_inspect_resolves_eq_joined_raw_arg_overlay(tmp_path):
    # Arrange — the =-joined spelling that once made an overlay invisible
    # to sac's narrower resolver; the detector must see every spelling.
    root = _overlay(tmp_path)
    _plant_dist_info(root, "scitex_cards", "0.16.1")
    ap = SimpleNamespace(
        overlay="", raw_args=[f"--overlay={root}"], image="", overlay_size=""
    )
    config = SimpleNamespace(apptainer=ap, workdir=str(root.parent), name="agent-x")
    # Act
    verdict = OM.inspect_agent_overlay("agent-x", config, lambda: _BASE)
    # Assert
    assert verdict.verdict == M.VERDICT_MASKED


# ---------------------------------------------------------------------------
# fleet sweep — one bad agent degrades to UNKNOWN, never kills the pass
# ---------------------------------------------------------------------------
def test_sweep_reports_one_verdict_per_agent(tmp_path):
    # Arrange — one agent whose declared overlay is missing (UNKNOWN before
    # any base consultation — keeps the test hermetic: the sweep must never
    # be driven into reading the REAL containers dir), one clean.
    missing_root = tmp_path / "overlays" / "agent-missing"
    clean_root = _overlay(tmp_path, "agent-clean")
    configs = [
        ("agent-missing", _config_for(missing_root)),
        ("agent-clean", _config_for(clean_root)),
    ]
    # Act
    verdicts = [v.verdict for v in OM.sweep_agent_overlays(configs)]
    # Assert
    assert verdicts == [M.VERDICT_UNKNOWN, M.VERDICT_CLEAN]


def test_sweep_survives_an_exploding_agent_config(tmp_path):
    # Arrange
    class _Boom:
        @property
        def apptainer(self):
            raise RuntimeError("malformed spec")

    clean_root = _overlay(tmp_path, "agent-clean")
    configs = [("agent-boom", _Boom()), ("agent-clean", _config_for(clean_root))]
    # Act
    verdicts = [v.verdict for v in OM.sweep_agent_overlays(configs)]
    # Assert — the bad agent is an UNKNOWN row, the good one still reported.
    assert verdicts == [M.VERDICT_UNKNOWN, M.VERDICT_CLEAN]


# ---------------------------------------------------------------------------
# base-set acquisition through a REAL fake apptainer on PATH
# ---------------------------------------------------------------------------
_FULL_PIP_LIST = json.dumps(
    [
        {"name": "scitex-cards", "version": "0.17.5"},
        {"name": "click", "version": "8.1.7"},
        {"name": "numpy", "version": "2.0.0"},
    ]
)


def _install_branching_apptainer(
    bin_dir: Path, env, *, piplist_ok: bool, manifest_ok: bool
) -> None:
    """A REAL fake ``apptainer`` on PATH branching on inner argv, exactly
    like ``_drift/test_versions.py`` does — no patched imports."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "apptainer"
    manifest = json.dumps([{"name": "scitex-cards", "version": "0.17.5"}])
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "inner = sys.argv[3:] if len(sys.argv) > 3 else []\n"
        "if 'cat' in sys.argv and any(a.endswith('scitex-versions.json') for a in sys.argv):\n"
        f"    ok = {manifest_ok!r}\n"
        f"    sys.stdout.write({manifest!r} if ok else '')\n"
        "    sys.exit(0 if ok else 1)\n"
        "if '--format=json' in sys.argv:\n"
        f"    ok = {piplist_ok!r}\n"
        f"    sys.stdout.write({_FULL_PIP_LIST!r} if ok else '')\n"
        "    sys.exit(0 if ok else 1)\n"
        "sys.exit(2)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    env.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_base_set_prefers_complete_live_pip_list(tmp_path, env_save_restore):
    # Arrange — a dummy SIF file the config points at directly.
    sif = tmp_path / "sac-scitex.sif"
    sif.write_bytes(b"SIF")
    _install_branching_apptainer(
        tmp_path / "bin", env_save_restore, piplist_ok=True, manifest_ok=True
    )
    config = SimpleNamespace(
        apptainer=SimpleNamespace(image=str(sif), overlay="", raw_args=[]),
        workdir=str(tmp_path),
    )
    # Act
    base = OM.base_package_set_for(config)
    # Assert — the UNFILTERED live read won: complete, and non-scitex rows kept.
    assert (base.complete, base.source, base.packages["numpy"]) == (
        True,
        "live",
        "2.0.0",
    )


def test_base_set_falls_back_to_partial_manifest(tmp_path, env_save_restore):
    # Arrange — live pip list fails; the baked manifest still answers.
    sif = tmp_path / "sac-scitex.sif"
    sif.write_bytes(b"SIF")
    _install_branching_apptainer(
        tmp_path / "bin", env_save_restore, piplist_ok=False, manifest_ok=True
    )
    config = SimpleNamespace(
        apptainer=SimpleNamespace(image=str(sif), overlay="", raw_args=[]),
        workdir=str(tmp_path),
    )
    # Act
    base = OM.base_package_set_for(config)
    # Assert — honestly labelled PARTIAL: a miss must classify base-unknown.
    assert (base.complete, base.source) == (False, "manifest")


def test_base_set_unreadable_returns_none(tmp_path, env_save_restore):
    # Arrange — both reads fail: the answer is "could not tell", never {}.
    sif = tmp_path / "sac-scitex.sif"
    sif.write_bytes(b"SIF")
    _install_branching_apptainer(
        tmp_path / "bin", env_save_restore, piplist_ok=False, manifest_ok=False
    )
    config = SimpleNamespace(
        apptainer=SimpleNamespace(image=str(sif), overlay="", raw_args=[]),
        workdir=str(tmp_path),
    )
    # Act
    base = OM.base_package_set_for(config)
    # Assert
    assert base is None
