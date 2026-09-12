from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_migrate_images import (
    migrate_images,
    migrate_spec_text,
)


def _spec(image: str) -> str:
    return (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  apptainer:\n"
        f"    image: {image}\n"
        "    binds: []\n"
    )


def test_migrate_spec_text_preserves_surrounding_bytes() -> None:
    # Arrange
    original = _spec("/scratch/u/sac-images/h/sac-base/sac-base-2026-0912-140710.sif")
    # Act
    migrated, old, logical = migrate_spec_text(original)
    # Assert
    assert (migrated, old, logical) == (
        _spec("sac-base"),
        "/scratch/u/sac-images/h/sac-base/sac-base-2026-0912-140710.sif",
        "sac-base",
    )


def test_migrate_spec_text_leaves_custom_same_basename_untouched() -> None:
    # Arrange
    original = _spec("/tmp/ci-build/sac-base.sif")
    # Act
    result = migrate_spec_text(original)
    # Assert
    assert result == (original, None, None)


def test_migrate_images_dry_run_does_not_write(tmp_path: Path) -> None:
    # Arrange
    spec = tmp_path / "alpha" / "spec.yaml"
    spec.parent.mkdir()
    original = _spec("~/.scitex/agent-container/containers/sac-base.sif")
    spec.write_text(original)
    # Act
    result = CliRunner().invoke(migrate_images, ["--root", str(tmp_path), "--json"])
    # Assert
    assert (
        result.exit_code,
        json.loads(result.output)["migrations_required"],
        spec.read_text(),
    ) == (0, 1, original)


def test_migrate_images_apply_is_atomic_and_check_becomes_clean(tmp_path: Path) -> None:
    # Arrange
    spec = tmp_path / "alpha" / "spec.yaml"
    spec.parent.mkdir()
    spec.write_text(
        _spec("/scratch/u/sac-images/h/sac-scitex/sac-scitex-2026-0912-140710.sif")
    )
    runner = CliRunner()
    # Act
    applied = runner.invoke(migrate_images, ["--root", str(tmp_path), "--apply"])
    checked = runner.invoke(migrate_images, ["--root", str(tmp_path), "--check"])
    # Assert
    assert (
        applied.exit_code,
        checked.exit_code,
        "image: sac-scitex" in spec.read_text(),
    ) == (0, 0, True)


def test_migrate_images_check_fails_when_work_remains(tmp_path: Path) -> None:
    # Arrange
    spec = tmp_path / "alpha" / "spec.yaml"
    spec.parent.mkdir()
    spec.write_text(_spec("~/.scitex/agent-container/containers/sac-proxy.sif"))
    # Act
    result = CliRunner().invoke(migrate_images, ["--root", str(tmp_path), "--check"])
    # Assert
    assert result.exit_code == 1
