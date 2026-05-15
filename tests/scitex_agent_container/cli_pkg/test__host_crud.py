"""CLI tests for ``sac host add / remove / set`` (peer CRUD).

PA-306: no ``unittest.mock``. Real ``CliRunner``, real ``tmp_path``
config.yaml surfaced via the ``SCITEX_AGENT_CONTAINER_CONFIG`` env
override. Each test follows AAA (TQ002), asserts a single fact
(TQ007), and uses a behaviour-shaped name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._host_crud import (
    host_add,
    host_remove,
    host_set,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def _read_peers(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}).get("peers") or {}


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_host_add_writes_ssh_target_to_yaml(cfg_path: Path):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert _read_peers(cfg_path)["gpu-box"]["ssh"] == "u@gpu.lan"


def test_host_add_exits_zero_on_success(cfg_path: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert result.exit_code == 0


def test_host_add_with_via_writes_list_under_via_key(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: {ssh: m}\n  spartan: {ssh: s}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["bm198", "--ssh", "bm198", "--via", "mba,spartan"])
    # Assert
    assert _read_peers(cfg_path)["bm198"]["via"] == ["mba", "spartan"]


def test_host_add_rejects_duplicate_name_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: old}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "new"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_host_remove_deletes_existing_peer_from_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: u@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_remove, ["gpu-box"])
    # Assert
    assert "gpu-box" not in _read_peers(cfg_path)


def test_host_remove_rejects_missing_peer_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_remove, ["nope"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def test_host_set_overwrites_ssh_target(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: old@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_set, ["gpu-box", "--ssh", "new@gpu"])
    # Assert
    assert _read_peers(cfg_path)["gpu-box"]["ssh"] == "new@gpu"


def test_host_set_writes_via_chain(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "peers:\n  mba: {ssh: m}\n  spartan: {ssh: s}\n  bm198: {ssh: bm198}\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_set, ["bm198", "--via", "mba,spartan"])
    # Assert
    assert _read_peers(cfg_path)["bm198"]["via"] == ["mba", "spartan"]


def test_host_set_rejects_missing_peer_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_set, ["nope", "--ssh", "x"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# round-trip comment preservation
# ---------------------------------------------------------------------------


def test_host_add_preserves_top_of_file_comment(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "# top comment\npeers:\n  foo:\n    ssh: bar  # inline comment\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["baz", "--ssh", "qux"])
    # Assert
    assert "# top comment" in cfg_path.read_text()


def test_host_add_preserves_inline_comment(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "# top comment\npeers:\n  foo:\n    ssh: bar  # inline comment\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["baz", "--ssh", "qux"])
    # Assert
    assert "# inline comment" in cfg_path.read_text()


# ---------------------------------------------------------------------------
# validation rollback
# ---------------------------------------------------------------------------


def test_host_add_reverts_file_when_validation_fails(cfg_path: Path):
    # Arrange — adding a peer whose via: hop is unknown trips
    # Config.validate; pre-edit bytes must survive intact.
    original = "peers: {}\n"
    cfg_path.write_text(original)
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["new-peer", "--ssh", "x", "--via", "ghost"])
    # Assert
    assert cfg_path.read_text() == original


def test_host_add_exits_nonzero_when_validation_fails(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["new-peer", "--ssh", "x", "--via", "ghost"])
    # Assert
    assert result.exit_code != 0
