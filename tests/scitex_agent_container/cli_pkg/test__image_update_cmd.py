"""Sandbox upgrades require approval; previews never load an execution backend."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg._main import main


@pytest.fixture
def update_backend(tmp_path):
    root = tmp_path / "sandbox with spaces"
    root.mkdir()
    calls = []

    class Backend:
        def sandbox_update(self, **kwargs):
            calls.append(kwargs)
            (root / "updated.json").write_text(json.dumps(list(kwargs["packages"])))
            return {"updated": list(kwargs["packages"])}

    def load():
        calls.append("backend-loaded")
        return Backend()

    previous = ig._load_apptainer
    ig._load_apptainer = load
    try:
        yield root, calls
    finally:
        ig._load_apptainer = previous


@pytest.mark.parametrize("flags", [[], ["-p", "numpy"]])
def test_update_refuses_without_approval_before_backend(update_backend, flags):
    # Arrange
    root, calls = update_backend
    # Act
    result = CliRunner().invoke(main, ["image", "update", str(root), *flags])
    # Assert
    assert (result.exit_code, "--yes" in result.output, calls) == (2, True, [])


@pytest.mark.parametrize(
    "flags", [["--dry-run"], ["--dry-run", "--yes"], ["-y", "--dry-run"]]
)
@pytest.mark.parametrize("packages", [[], ["-p", "numpy", "-p", "scipy"]])
def test_update_preview_never_loads_backend(update_backend, flags, packages):
    # Arrange
    root, calls = update_backend
    # Act
    result = CliRunner().invoke(main, ["image", "update", str(root), *packages, *flags])
    # Assert
    assert (result.exit_code, calls, (root / "updated.json").exists()) == (0, [], False)


def test_update_preview_describes_selected_packages(update_backend):
    # Arrange
    root, _ = update_backend
    # Act
    result = CliRunner().invoke(
        main, ["--json", "image", "update", str(root), "-p", "numpy", "--dry-run"]
    )
    # Assert
    assert json.loads(result.output) == {
        "action": "update",
        "dry_run": True,
        "sandbox_dir": str(root),
        "packages": ["numpy"],
    }


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_approved_update_keeps_default_packages_and_result(update_backend, approval):
    # Arrange
    root, _ = update_backend
    # Act
    result = CliRunner().invoke(main, ["image", "update", str(root), approval])
    # Assert
    assert json.loads(result.output) == {"updated": ["scitex[all]"]}


def test_approved_update_forwards_ordered_packages_once(update_backend):
    # Arrange
    root, calls = update_backend
    # Act
    CliRunner().invoke(
        main,
        [
            "image",
            "update",
            str(root),
            "-p",
            "numpy",
            "-p",
            "numpy",
            "-p",
            "scipy",
            "--yes",
        ],
    )
    # Assert
    assert calls == [
        "backend-loaded",
        {"sandbox_dir": root, "packages": ("numpy", "numpy", "scipy")},
    ]
