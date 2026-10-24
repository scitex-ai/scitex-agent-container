"""Tests for ``sac image prune`` — SIF retention (keep 3 + current target).

Real filesystem paths against a tmp-rooted ``$HOME`` (same save/restore
pattern as ``test_image_group.home_tmp``). No mocks: dated ``.sif``
fixtures are real files with controlled mtimes; the current target is a
real symlink.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg._image_prune_cmd import plan_image_prune
from scitex_agent_container.cli_pkg.image_group import image_group


@pytest.fixture
def home_tmp(tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    saved_containers_dir = ig._CONTAINERS_DIR
    saved_state_root = ig._SCITEX_USER_STATE_ROOT
    ig._CONTAINERS_DIR = home / ".scitex" / "agent-container" / "containers"  # type: ignore[assignment]
    ig._SCITEX_USER_STATE_ROOT = home / ".scitex"  # type: ignore[assignment]
    try:
        yield tmp_path
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        ig._CONTAINERS_DIR = saved_containers_dir  # type: ignore[assignment]
        ig._SCITEX_USER_STATE_ROOT = saved_state_root  # type: ignore[assignment]


def _layer_with_five(home_tmp: Path, *, current: str) -> Path:
    """Five dated SIFs (oldest first) + current symlink + sidecars/noise."""
    layer = Path(ig._CONTAINERS_DIR) / "sac-base"
    layer.mkdir(parents=True)
    names = [
        "sac-base-2026-1001-000001.sif",
        "sac-base-2026-1002-000002.sif",
        "sac-base-2026-1003-000003.sif",
        "sac-base-2026-1004-000004.sif",
        "sac-base-2026-1005-000005.sif",
    ]
    base = 1_700_000_000.0
    for i, name in enumerate(names):
        p = layer / name
        p.write_bytes(b"sif-bytes")
        os.utime(p, (base + i * 100.0, base + i * 100.0))
        (layer / (name + ".sha256")).write_text("deadbeef  " + name + "\n")
    (layer / "my-hand-test.sif").write_bytes(b"custom")
    (layer / "sac-base-2026-1001-000001.sif.build.log").write_text("log\n")
    link = Path(ig._CONTAINERS_DIR) / "sac-base.sif"
    link.symlink_to(layer / current)
    return layer


def test_prune_policy_keeps_current_target_even_when_oldest() -> None:
    # Arrange
    files = [(f"sac-base-2026-100{i}-00000{i}.sif", 1000.0 + i) for i in range(1, 6)]
    # Act
    keep, _ = plan_image_prune(
        files, current="sac-base-2026-1001-000001.sif", keep=3
    )
    # Assert
    assert "sac-base-2026-1001-000001.sif" in keep


def test_prune_policy_keeps_two_newest_besides_current() -> None:
    # Arrange
    files = [(f"sac-base-2026-100{i}-00000{i}.sif", 1000.0 + i) for i in range(1, 6)]
    # Act
    keep, prune = plan_image_prune(
        files, current="sac-base-2026-1001-000001.sif", keep=3
    )
    # Assert
    assert sorted(keep) == sorted(
        [
            "sac-base-2026-1001-000001.sif",
            "sac-base-2026-1004-000004.sif",
            "sac-base-2026-1005-000005.sif",
        ]
    )


def test_prune_policy_rejects_keep_below_one() -> None:
    # Arrange
    # Act
    act = lambda: plan_image_prune([], current=None, keep=0)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="keep"):
        act()


def test_prune_refuses_without_dry_run_or_yes(home_tmp: Path) -> None:
    # Arrange
    _layer_with_five(home_tmp, current="sac-base-2026-1005-000005.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["prune"])
    # Assert
    assert result.exit_code == 2 and "Refusing" in result.output


def test_prune_dry_run_lists_candidates_and_deletes_nothing(
    home_tmp: Path,
) -> None:
    # Arrange
    layer = _layer_with_five(home_tmp, current="sac-base-2026-1005-000005.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["prune", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and sorted(
        p.name for p in layer.glob("*.sif") if not p.is_symlink()
    ) == sorted(
        [
            "sac-base-2026-1001-000001.sif",
            "sac-base-2026-1002-000002.sif",
            "sac-base-2026-1003-000003.sif",
            "sac-base-2026-1004-000004.sif",
            "sac-base-2026-1005-000005.sif",
            "my-hand-test.sif",
        ]
    )


def test_prune_dry_run_names_the_two_oldest_dated_candidates(
    home_tmp: Path,
) -> None:
    # Arrange
    _layer_with_five(home_tmp, current="sac-base-2026-1005-000005.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["prune", "--dry-run"])
    # Assert
    assert (
        "sac-base-2026-1001-000001.sif" in result.output
        and "sac-base-2026-1002-000002.sif" in result.output
    )


def test_prune_yes_deletes_candidates_sidecars_and_keeps_rest(
    home_tmp: Path,
) -> None:
    # Arrange
    layer = _layer_with_five(home_tmp, current="sac-base-2026-1005-000005.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["prune", "--yes"])
    # Assert
    assert result.exit_code == 0 and sorted(
        p.name for p in layer.iterdir()
    ) == sorted(
        [
            "sac-base-2026-1003-000003.sif",
            "sac-base-2026-1003-000003.sif.sha256",
            "sac-base-2026-1004-000004.sif",
            "sac-base-2026-1004-000004.sif.sha256",
            "sac-base-2026-1005-000005.sif",
            "sac-base-2026-1005-000005.sif.sha256",
            "my-hand-test.sif",
            "sac-base-2026-1001-000001.sif.build.log",
        ]
    )
