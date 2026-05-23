"""Tests for runtimes/_apptainer_tmpfs.py — sized /tmp scratch helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_tmpfs import (
    TmpfsSpaceError,
    parse_tmpfs_size_bytes,
    tmpfs_workdir_flags,
)

# ---------------------------------------------------------------------------
# parse_tmpfs_size_bytes
# ---------------------------------------------------------------------------


def test_parse_2g_resolves_to_two_gibibytes() -> None:
    # Arrange
    size = "2G"
    # Act
    nbytes = parse_tmpfs_size_bytes(size)
    # Assert
    assert nbytes == 2 * 1024**3


def test_parse_512m_resolves_to_megabytes() -> None:
    # Arrange
    size = "512M"
    # Act
    nbytes = parse_tmpfs_size_bytes(size)
    # Assert
    assert nbytes == 512 * 1024**2


def test_parse_gb_unit_equivalent_to_g() -> None:
    # Arrange
    size = "1GB"
    # Act
    nbytes = parse_tmpfs_size_bytes(size)
    # Assert
    assert nbytes == 1024**3


def test_parse_rejects_kilobytes() -> None:
    # Arrange — K/KB are below the 1MB floor and unsupported.
    size = "500K"
    # Act
    ctx = pytest.raises(ValueError)
    # Assert
    with ctx:
        parse_tmpfs_size_bytes(size)


def test_parse_rejects_unparseable() -> None:
    # Arrange
    size = "big"
    # Act
    ctx = pytest.raises(ValueError)
    # Assert
    with ctx:
        parse_tmpfs_size_bytes(size)


# ---------------------------------------------------------------------------
# tmpfs_workdir_flags
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal config stand-in exposing only ``apptainer``."""

    def __init__(self, apptainer: ApptainerSpec | None) -> None:
        self.apptainer = apptainer


def test_flags_default_emits_workdir(tmp_path: Path) -> None:
    # Arrange — default ApptainerSpec → tmpfs_size "2G".
    cfg = _Cfg(ApptainerSpec())
    # Act
    flags = tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert flags == ["--workdir", str(tmp_path / "tmp-scratch")]


def test_flags_creates_scratch_dir(tmp_path: Path) -> None:
    # Arrange
    cfg = _Cfg(ApptainerSpec())
    # Act
    tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert (tmp_path / "tmp-scratch").is_dir()


def test_flags_empty_size_opts_out(tmp_path: Path) -> None:
    # Arrange — explicit "" disables the workdir relocation.
    cfg = _Cfg(ApptainerSpec(tmpfs_size=""))
    # Act
    flags = tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert flags == []


def test_flags_no_apptainer_block_still_defaults(tmp_path: Path) -> None:
    # Arrange — a config without an apptainer block gets the 2G default.
    cfg = _Cfg(None)
    # Act
    flags = tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert flags == ["--workdir", str(tmp_path / "tmp-scratch")]


def test_flags_skips_when_operator_declares_workdir(tmp_path: Path) -> None:
    # Arrange — operator's own --workdir in raw_args takes precedence.
    cfg = _Cfg(ApptainerSpec(raw_args=["--workdir", "/scratch/mine"]))
    # Act
    flags = tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert flags == []


def test_flags_skips_when_operator_declares_short_workdir(tmp_path: Path) -> None:
    # Arrange — the short -W form is recognised too.
    cfg = _Cfg(ApptainerSpec(raw_args=["-W", "/scratch/mine"]))
    # Act
    flags = tmpfs_workdir_flags(cfg, tmp_path)
    # Assert
    assert flags == []


def test_flags_fail_loud_when_space_insufficient(tmp_path: Path) -> None:
    # Arrange — request more than any real filesystem can offer so the
    # free-space guard fires deterministically (10 EiB exceeds every disk).
    cfg = _Cfg(ApptainerSpec(tmpfs_size="10737418240G"))
    # Act
    ctx = pytest.raises(TmpfsSpaceError)
    # Assert
    with ctx:
        tmpfs_workdir_flags(cfg, tmp_path)


def test_flags_propagates_parse_error_on_bad_size(tmp_path: Path) -> None:
    # Arrange
    cfg = _Cfg(ApptainerSpec(tmpfs_size="garbage"))
    # Act
    ctx = pytest.raises(ValueError)
    # Assert
    with ctx:
        tmpfs_workdir_flags(cfg, tmp_path)
