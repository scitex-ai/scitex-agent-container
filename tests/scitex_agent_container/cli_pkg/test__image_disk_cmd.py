"""Tests for ``sac image disk`` — filesystem pressure + threshold alert.

Real ``shutil.disk_usage`` values against tmp paths (no mocks): a 0.0
threshold always fires, a 101.0 threshold can never fire, so both
directions are deterministic on any host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg._image_disk_cmd import (
    disk_pressure_pct,
    pressure_fires,
)
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
    Path(ig._CONTAINERS_DIR).mkdir(parents=True, exist_ok=True)
    try:
        yield tmp_path
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        ig._CONTAINERS_DIR = saved_containers_dir  # type: ignore[assignment]
        ig._SCITEX_USER_STATE_ROOT = saved_state_root  # type: ignore[assignment]


def test_pressure_pct_is_a_real_percentage(home_tmp: Path) -> None:
    # Arrange
    # Act
    pct = disk_pressure_pct(Path(ig._CONTAINERS_DIR))
    # Assert
    assert 0.0 <= pct <= 100.0


def test_pressure_fires_at_or_above_threshold() -> None:
    # Arrange
    # Act
    fires = pressure_fires(85.0, threshold=85.0)
    # Assert
    assert fires is True


def test_pressure_quiet_below_threshold() -> None:
    # Arrange
    # Act
    fires = pressure_fires(84.9, threshold=85.0)
    # Assert
    assert fires is False


def test_disk_quiet_on_impossible_threshold(home_tmp: Path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["disk", "--alert-threshold", "101"])
    # Assert
    assert result.exit_code == 0 and "ALERT" not in result.output


def test_disk_fires_on_zero_test_threshold(home_tmp: Path) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(image_group, ["disk", "--alert-threshold", "0"])
    # Assert
    assert result.exit_code == 0 and "ALERT" in result.output
