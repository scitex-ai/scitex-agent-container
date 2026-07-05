"""Tests for scitex-* version introspection across sac's image layers.

PA-306: no mocks. The base-image exec path runs a REAL fake ``apptainer``
binary installed on PATH (a self-contained Python script that branches on
argv exactly like the real one would — ``cat <manifest>`` vs
``pip list --format=json``), so production code hits the real
``subprocess.run`` + real PATH lookup. The overlay + filesystem paths run
against real temp directories (fake venv dist-info dirs, real overlay
uppers) — no patched imports.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._drift import versions as V

# A pip `list --format=json` payload mixing scitex-* and non-scitex rows.
_PIP_LIST = json.dumps(
    [
        {"name": "scitex", "version": "2.11.0"},
        {"name": "scitex-io", "version": "1.2.3"},
        {"name": "scitex_plt", "version": "0.9.0"},
        {"name": "numpy", "version": "2.0.0"},
        {"name": "click", "version": "8.1.7"},
    ]
)

# A baked manifest (already scitex-filtered, pip's own shape).
_MANIFEST = json.dumps(
    [
        {"name": "scitex", "version": "9.9.9"},
        {"name": "scitex-io", "version": "9.9.9"},
    ]
)


def _install_fake_apptainer(
    bin_dir: Path,
    env,
    *,
    piplist: str = _PIP_LIST,
    manifest: str = _MANIFEST,
    manifest_stems: tuple[str, ...] = (),
) -> None:
    """Install a branching fake ``apptainer`` on PATH.

    ``manifest_stems`` names the SIF stems whose ``cat <manifest>`` succeeds;
    any other stem's ``cat`` exits 1 (manifest absent → live fallback).
    ``pip list --format=json`` always returns ``piplist``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "apptainer"
    body = (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "argv = sys.argv[1:]\n"
        "sif = argv[1] if len(argv) > 1 else ''\n"
        "inner = argv[2:]\n"
        "stem = os.path.basename(sif)\n"
        "stem = stem[:-4] if stem.endswith('.sif') else stem\n"
        f"manifest_stems = {list(manifest_stems)!r}\n"
        "if inner[:1] == ['cat'] and inner and inner[-1].endswith('scitex-versions.json'):\n"
        "    if stem in manifest_stems:\n"
        f"        sys.stdout.write({manifest!r}); sys.exit(0)\n"
        "    sys.stderr.write('cat: no such file\\n'); sys.exit(1)\n"
        "if 'list' in inner and '--format=json' in inner:\n"
        f"    sys.stdout.write({piplist!r}); sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    env.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _make_venv_dist_info(venv_root: Path, pkgs: dict[str, str]) -> None:
    """Create ``<venv>/lib/python3.12/site-packages/<Name>-<Version>.dist-info``."""
    site = venv_root / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, version in pkgs.items():
        (site / f"{name}-{version}.dist-info").mkdir()


def _fake_config(overlay_dir: Path, image: str = "") -> SimpleNamespace:
    ap = SimpleNamespace(overlay=str(overlay_dir), raw_args=[], image=image)
    return SimpleNamespace(apptainer=ap, workdir=str(overlay_dir.parent))


# ---------------------------------------------------------------------------
# pure helpers: scitex filter + normalisation + overlay diff
# ---------------------------------------------------------------------------
def test_is_scitex_matches_core_and_dashed():
    # Arrange
    names = ["scitex", "scitex-io", "scitex_plt"]
    # Act
    results = [V.is_scitex_package(n) for n in names]
    # Assert
    assert results == [True, True, True]


def test_is_scitex_rejects_non_ecosystem():
    # Arrange
    non_ecosystem = ["numpy", "scitexfoo", ""]
    # Act
    results = [V.is_scitex_package(n) for n in non_ecosystem]
    # Assert
    assert not any(results)


def test_normalize_filters_to_scitex_only():
    # Arrange
    raw = json.loads(_PIP_LIST)
    # Act
    rows = V.normalize_pkg_list(raw)
    # Assert
    assert rows == [
        {"package": "scitex", "version": "2.11.0"},
        {"package": "scitex-io", "version": "1.2.3"},
        {"package": "scitex-plt", "version": "0.9.0"},
    ]


def test_normalize_accepts_mapping_shape():
    # Arrange
    raw = {"scitex-io": "1.0.0", "numpy": "2.0.0"}
    # Act
    rows = V.normalize_pkg_list(raw)
    # Assert
    assert rows == [{"package": "scitex-io", "version": "1.0.0"}]


def test_overlay_adds_keeps_only_adds_and_overrides():
    # Arrange
    base = {"scitex-io": "1.0.0", "scitex": "2.0.0"}
    overlay = [
        {"package": "scitex-io", "version": "9.9.9"},  # override
        {"package": "scitex", "version": "2.0.0"},  # identical → dropped
        {"package": "scitex-cv", "version": "0.5.0"},  # add
    ]
    # Act
    kept = V.overlay_adds(overlay, base)
    # Assert
    assert kept == [
        {"package": "scitex-io", "version": "9.9.9"},
        {"package": "scitex-cv", "version": "0.5.0"},
    ]


# ---------------------------------------------------------------------------
# agent → base image name mapping
# ---------------------------------------------------------------------------
def test_base_image_name_defaults_when_empty():
    # Arrange
    cfg = _fake_config(Path("/x/overlay"), image="")
    # Act
    name = V.agent_base_image_name(cfg)
    # Assert
    assert name == "sac-scitex"


def test_base_image_name_from_sif_path():
    # Arrange
    cfg = _fake_config(Path("/x/overlay"), image="/opt/containers/sac-base.sif")
    # Act
    name = V.agent_base_image_name(cfg)
    # Assert
    assert name == "sac-base"


def test_base_image_name_strips_docker_tag():
    # Arrange
    cfg = _fake_config(Path("/x/overlay"), image="docker://org/sac-scitex:v2")
    # Act
    name = V.agent_base_image_name(cfg)
    # Assert
    assert name == "sac-scitex"


# ---------------------------------------------------------------------------
# base SIF discovery (top-level + nested symlinks, deduped)
# ---------------------------------------------------------------------------
def test_discover_finds_toplevel_and_nested(tmp_path: Path):
    # Arrange
    (tmp_path / "sac-base.sif").write_text("")  # top-level symlink form
    nested = tmp_path / "sac-scitex"
    nested.mkdir()
    (nested / "sac-scitex.sif").write_text("")  # nested boot symlink form
    # Act
    found = V.discover_base_sifs(tmp_path)
    # Assert
    assert [name for name, _ in found] == ["sac-base", "sac-scitex"]


def test_discover_empty_dir_returns_empty(tmp_path: Path):
    # Arrange
    missing = tmp_path / "missing"
    # Act
    found = V.discover_base_sifs(missing)
    # Assert
    assert found == []


# ---------------------------------------------------------------------------
# base-image rows via the real fake-apptainer exec seam
# ---------------------------------------------------------------------------
def test_base_rows_prefer_manifest_source(tmp_path: Path, env_save_restore):
    # Arrange
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(
        tmp_path / "bin", env_save_restore, manifest_stems=("sac-base",)
    )
    # Act
    rows, base_maps = V.base_image_rows(tmp_path)
    # Assert
    assert all(r["source"] == "manifest" for r in rows) and base_maps["sac-base"] == {
        "scitex": "9.9.9",
        "scitex-io": "9.9.9",
    }


def test_base_rows_fall_back_to_live(tmp_path: Path, env_save_restore):
    # Arrange — no stem has a manifest, so cat fails → live pip list.
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(tmp_path / "bin", env_save_restore, manifest_stems=())
    # Act
    rows, _ = V.base_image_rows(tmp_path)
    # Assert
    assert rows and all(r["source"] == "live" for r in rows)


def test_base_rows_live_flag_forces_live(tmp_path: Path, env_save_restore):
    # Arrange — manifest IS present but --live must bypass it.
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(
        tmp_path / "bin", env_save_restore, manifest_stems=("sac-base",)
    )
    # Act
    rows, _ = V.base_image_rows(tmp_path, live=True)
    # Assert
    assert rows and all(r["source"] == "live" for r in rows)


def test_base_rows_carry_full_contract_shape(tmp_path: Path, env_save_restore):
    # Arrange
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(tmp_path / "bin", env_save_restore, manifest_stems=())
    # Act
    rows, _ = V.base_image_rows(tmp_path)
    # Assert
    assert all(
        set(r) == {"agent", "layer", "image", "package", "version", "source"}
        and r["agent"] == "*"
        and r["layer"] == "base-image"
        and r["image"] == "sac-base"
        for r in rows
    )


# ---------------------------------------------------------------------------
# overlay venv scan + overlay rows
# ---------------------------------------------------------------------------
def test_scan_venv_returns_scitex_dist_info(tmp_path: Path):
    # Arrange
    _make_venv_dist_info(
        tmp_path, {"scitex_io": "1.2.3", "numpy": "2.0.0", "scitex": "2.11.0"}
    )
    # Act
    rows = V.scan_venv_scitex(tmp_path)
    # Assert
    assert rows == [
        {"package": "scitex", "version": "2.11.0"},
        {"package": "scitex-io", "version": "1.2.3"},
    ]


def test_scan_venv_missing_root_is_empty(tmp_path: Path):
    # Arrange
    absent = tmp_path / "nope"
    # Act
    rows = V.scan_venv_scitex(absent)
    # Assert
    assert rows == []


def test_overlay_rows_emit_only_adds_and_overrides(tmp_path: Path):
    # Arrange — overlay venv adds scitex-cv, overrides scitex-io, matches scitex.
    overlay = tmp_path / "overlays" / "worker"
    _make_venv_dist_info(
        overlay / "upper" / "opt" / "venv-sac",
        {"scitex_io": "9.9.9", "scitex": "2.0.0", "scitex_cv": "0.5.0"},
    )
    base_maps = {"sac-scitex": {"scitex-io": "1.0.0", "scitex": "2.0.0"}}
    cfg = _fake_config(overlay)
    # Act
    rows = V.agent_overlay_rows([("worker", cfg)], base_maps, live=True)
    # Assert
    assert sorted((r["package"], r["version"]) for r in rows) == [
        ("scitex-cv", "0.5.0"),
        ("scitex-io", "9.9.9"),
    ]


def test_overlay_rows_shape_and_source_live(tmp_path: Path):
    # Arrange
    overlay = tmp_path / "overlays" / "worker"
    _make_venv_dist_info(
        overlay / "upper" / "opt" / "venv-sac", {"scitex_cv": "0.5.0"}
    )
    cfg = _fake_config(overlay, image="/c/sac-base.sif")
    # Act
    rows = V.agent_overlay_rows([("worker", cfg)], {}, live=True)
    # Assert
    assert rows == [
        {
            "agent": "worker",
            "layer": "agent-overlay",
            "image": "sac-base",
            "package": "scitex-cv",
            "version": "0.5.0",
            "source": "live",
        }
    ]


def test_overlay_rows_prefer_manifest_source(tmp_path: Path):
    # Arrange — a recorded overlay manifest should be read as source=manifest.
    overlay = tmp_path / "overlays" / "worker"
    overlay.mkdir(parents=True)
    (overlay / "scitex-overlay-versions.json").write_text(
        json.dumps([{"name": "scitex-cv", "version": "0.7.0"}])
    )
    cfg = _fake_config(overlay)
    # Act
    rows = V.agent_overlay_rows([("worker", cfg)], {}, live=False)
    # Assert
    assert rows[0]["source"] == "manifest" and rows[0]["version"] == "0.7.0"


def test_overlay_rows_no_overlay_yields_nothing(tmp_path: Path):
    # Arrange — config with no overlay declared.
    cfg = SimpleNamespace(
        apptainer=SimpleNamespace(overlay="", raw_args=[], image=""),
        workdir=str(tmp_path),
    )
    # Act
    rows = V.agent_overlay_rows([("worker", cfg)], {}, live=True)
    # Assert
    assert rows == []


# ---------------------------------------------------------------------------
# record side + top-level assembly
# ---------------------------------------------------------------------------
def test_record_overlay_manifest_writes_scitex_set(tmp_path: Path):
    # Arrange
    overlay = tmp_path / "overlays" / "worker"
    _make_venv_dist_info(
        overlay / "upper" / "opt" / "venv-sac", {"scitex_cv": "0.5.0"}
    )
    cfg = _fake_config(overlay)
    # Act
    path = V.record_overlay_manifest(cfg)
    # Assert
    assert json.loads(Path(path).read_text()) == [
        {"package": "scitex-cv", "version": "0.5.0"}
    ]


def test_collect_versions_combines_base_and_overlay(tmp_path: Path, env_save_restore):
    # Arrange
    (tmp_path / "sac-scitex.sif").write_text("")
    _install_fake_apptainer(tmp_path / "bin", env_save_restore, manifest_stems=())
    overlay = tmp_path / "overlays" / "worker"
    _make_venv_dist_info(
        overlay / "upper" / "opt" / "venv-sac", {"scitex_cv": "0.5.0"}
    )
    cfg = _fake_config(overlay, image="/c/sac-scitex.sif")
    # Act
    rows = V.collect_versions(
        live=True, containers_dir=tmp_path, agent_configs=[("worker", cfg)]
    )
    # Assert — flat list carries both layers, every row the 6-key contract.
    layers = {r["layer"] for r in rows}
    assert layers == {"base-image", "agent-overlay"} and all(
        set(r) == {"agent", "layer", "image", "package", "version", "source"}
        for r in rows
    )


def test_collect_versions_base_only_when_no_agents(tmp_path: Path, env_save_restore):
    # Arrange
    (tmp_path / "sac-base.sif").write_text("")
    _install_fake_apptainer(tmp_path / "bin", env_save_restore, manifest_stems=())
    # Act
    rows = V.collect_versions(live=True, containers_dir=tmp_path, agent_configs=[])
    # Assert
    assert rows and {r["layer"] for r in rows} == {"base-image"}
