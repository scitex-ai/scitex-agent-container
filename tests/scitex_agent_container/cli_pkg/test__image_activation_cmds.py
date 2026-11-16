"""Image activation approval gates and real, isolated SAC link changes."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main
from scitex_agent_container.cli_pkg import _image_activation
from scitex_agent_container.cli_pkg import image_group as ig


@pytest.fixture
def forbidden_activation():
    originals = {
        name: getattr(_image_activation, name)
        for name in ("switch_layer_version", "rollback_layer")
    }
    calls = []

    def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("activation must not run during preview or refusal")

    _image_activation.switch_layer_version = forbidden
    _image_activation.rollback_layer = forbidden
    try:
        yield calls
    finally:
        for name, value in originals.items():
            setattr(_image_activation, name, value)


@pytest.mark.parametrize("args", [["switch", "2026-0101-010000"], ["rollback"]])
@pytest.mark.parametrize("flags", [[], ["--yes", "--dry-run"], ["-y", "--dry-run"]])
def test_unapproved_or_conflicting_activation_never_reaches_backend(
    forbidden_activation, args, flags
):
    # Arrange
    # Act
    result = CliRunner().invoke(main, ["image", *args, *flags])
    # Assert
    assert (result.exit_code, "--yes" in result.output, forbidden_activation) == (
        2,
        True,
        [],
    )


@pytest.mark.parametrize(
    "args, action, version",
    [
        (["switch", "../unverified-version"], "switch", "../unverified-version"),
        (["rollback"], "rollback", None),
    ],
)
def test_preview_reports_arguments_without_backend_or_version_selection(
    forbidden_activation, args, action, version
):
    # Arrange
    # Act
    result = CliRunner().invoke(main, ["image", *args, "--layer", "proxy", "--dry-run"])
    # Assert
    assert (result.exit_code, json.loads(result.output), forbidden_activation) == (
        0,
        {
            "action": action,
            "layer": "proxy",
            "version": version,
            "dry_run": True,
            "scope": "arguments_only",
            "verified": False,
        },
        [],
    )


@pytest.fixture
def layer_store(tmp_path: Path):
    previous = ig._CONTAINERS_DIR
    root = tmp_path / "containers"
    for layer in _image_activation.LAYERS:
        directory = root / f"sac-{layer}"
        directory.mkdir(parents=True)
        for index, version in enumerate(
            ("2026-0101-010000", "2026-0101-020000", "2026-0101-030000"), start=1
        ):
            artifact = directory / f"sac-{layer}-{version}.sif"
            artifact.write_bytes(version.encode())
            os.utime(artifact, ns=(index, index))
        current = f"sac-{layer}-2026-0101-030000.sif"
        (directory / f"sac-{layer}.sif").symlink_to(current)
        (root / f"sac-{layer}.sif").symlink_to(Path(f"sac-{layer}") / current)
    ig._CONTAINERS_DIR = root
    try:
        yield root
    finally:
        ig._CONTAINERS_DIR = previous


@pytest.mark.parametrize("layer", _image_activation.LAYERS)
@pytest.mark.parametrize("approval", ["--yes", "-y"])
@pytest.mark.parametrize(
    "args, action, expected",
    [
        (["switch", "2026-0101-010000"], "switch", "2026-0101-010000"),
        (["rollback"], "rollback", "2026-0101-020000"),
    ],
)
def test_approved_activation_updates_both_real_links_and_reports_result(
    layer_store, layer, approval, args, action, expected
):
    # Arrange
    directory = layer_store / f"sac-{layer}"
    target = directory / f"sac-{layer}-{expected}.sif"
    # Act
    result = CliRunner().invoke(
        main, ["image", *args, "--layer", layer, approval, "--json"]
    )
    # Assert
    assert (
        result.exit_code,
        json.loads(result.output),
        (directory / f"sac-{layer}.sif").resolve(),
        (layer_store / f"sac-{layer}.sif").resolve(),
    ) == (0, {"action": action, "layer": layer, "version": expected}, target, target)


@pytest.mark.parametrize("args", [["switch", "2026-0101-010000"], ["rollback"]])
def test_root_json_reports_approved_activation_without_rich_text(layer_store, args):
    # Arrange
    # Act
    result = CliRunner().invoke(main, ["--json", "image", *args, "--yes"])
    # Assert
    assert (result.exit_code, json.loads(result.output)["layer"]) == (0, "base")


@pytest.mark.parametrize("args", [["switch", "2026-0101-010000"], ["rollback"]])
def test_preview_does_not_modify_existing_live_links(layer_store, args):
    # Arrange
    top = layer_store / "sac-base.sif"
    inner = layer_store / "sac-base/sac-base.sif"
    before = (os.readlink(top), os.readlink(inner))
    # Act
    result = CliRunner().invoke(main, ["image", *args, "--dry-run"])
    # Assert
    assert (result.exit_code, os.readlink(top), os.readlink(inner)) == (0, *before)
