"""Tests for ``sac agents archive-claude-bloat`` (F-CS8 closure, 2026-06-13).

Real I/O against ``tmp_path`` workdirs with fabricated bloat layouts.
No mocks, no ``monkeypatch``. AAA markers (STX-TQ002) + one assertion
per test (STX-TQ007). The audit-driven move pipeline is exercised by
constructing real ``.claude/<rel_path>/`` trees with enough stub files
to trip the per-subdir bloat threshold (default 1,000 files).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agents_prune_claude import (
    archive_bloat_sources,
    archive_claude_bloat,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _populate_subdir(claude: Path, rel: str, file_count: int) -> Path:
    """Create ``claude/rel`` with ``file_count`` 1-byte stub files.

    Returns the populated directory path. ``file_count`` is set above
    the default per-subdir bloat threshold (1,000) so the audit's
    ``bloat_sources`` list includes the entry without needing env
    overrides.
    """
    target = claude / rel
    target.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        (target / f"f{i}").write_bytes(b"x")
    return target


def _make_bloated_workdir(tmp_path: Path) -> Path:
    """Build a workdir whose ``.claude/`` carries a single bloat source.

    Mirrors the observed failure-mode shape: a ``hooks/pre-tool-use/
    .pending/`` directory with ~1,500 stub files (above the 1,000-file
    per-subdir threshold so the audit flags it). 1,500 stays small
    enough that the test stays sub-second on the CI runner.
    """
    workdir = tmp_path / "wd"
    workdir.mkdir()
    claude = workdir / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks/pre-tool-use/.pending", 1_500)
    return workdir


# ---------------------------------------------------------------------------
# archive_bloat_sources — pure function, no CLI
# ---------------------------------------------------------------------------


def test_archive_returns_one_record_per_bloat_source(tmp_path: Path):
    # Arrange — workdir with a single bloat source (the .pending bucket).
    workdir = _make_bloated_workdir(tmp_path)
    # Act
    records = archive_bloat_sources(workdir)
    # Assert
    assert len(records) == 1


def test_archive_creates_archive_root_under_dot_claude(tmp_path: Path):
    # Arrange
    workdir = _make_bloated_workdir(tmp_path)
    # Act
    archive_bloat_sources(workdir)
    archive_roots = list((workdir / ".claude").glob(".archived-*"))
    # Assert
    assert len(archive_roots) == 1


def test_archive_moves_source_out_of_original_location(tmp_path: Path):
    # Arrange — original bloat lives at .claude/hooks/pre-tool-use/.pending/.
    workdir = _make_bloated_workdir(tmp_path)
    original = workdir / ".claude" / "hooks" / "pre-tool-use" / ".pending"
    # Act
    archive_bloat_sources(workdir)
    # Assert
    assert not original.exists()


def test_archive_destination_contains_moved_tree(tmp_path: Path):
    # Arrange
    workdir = _make_bloated_workdir(tmp_path)
    # Act
    records = archive_bloat_sources(workdir)
    dest = Path(records[0]["to"])
    moved_files = list(dest.iterdir())
    # Assert — every stub file from the source is now under the archive.
    assert len(moved_files) == 1_500


def test_archive_record_carries_audit_file_count(tmp_path: Path):
    # Arrange
    workdir = _make_bloated_workdir(tmp_path)
    # Act
    records = archive_bloat_sources(workdir)
    # Assert
    assert records[0]["files"] == 1_500


def test_archive_returns_empty_when_no_bloat_sources(tmp_path: Path):
    # Arrange — workdir with only a tiny .claude/ tree (well under threshold).
    workdir = tmp_path / "wd"
    workdir.mkdir()
    claude = workdir / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks", 5)
    # Act
    records = archive_bloat_sources(workdir)
    # Assert
    assert records == []


def test_archive_destination_path_lives_under_archive_root(tmp_path: Path):
    # Arrange
    workdir = _make_bloated_workdir(tmp_path)
    # Act
    records = archive_bloat_sources(workdir)
    archive_root = next((workdir / ".claude").glob(".archived-*"))
    dest = Path(records[0]["to"])
    # Assert — destination is inside the single per-run archive bucket.
    assert dest.is_relative_to(archive_root)


# ---------------------------------------------------------------------------
# CLI command — Click runner against a real registered agent
# ---------------------------------------------------------------------------


@pytest.fixture
def _registered(tmp_path: Path, env_save_restore):
    """Register a real agent whose workdir is the bloated tmp tree."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))

    workdir = _make_bloated_workdir(tmp_path)

    name = "archive-target"
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    spec_path = agents_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": explicit_spec(
                    {
                        "runtime": "apptainer",
                        "host": "${HOSTNAME}",
                        "workdir": str(workdir),
                        "apptainer": {"image": "/x.sif", "binds": []},
                        "claude": {"model": "sonnet"},
                        "health": {"enabled": True, "interval": 60},
                        "restart": {"policy": "on-failure", "max_retries": 3},
                    }
                ),
            }
        )
    )

    from scitex_agent_container._state.registry import Registry

    Registry().add(name=name, config_path=str(spec_path), screen_name=name)
    return name, workdir


def test_cli_exits_zero_on_successful_archive(_registered):
    # Arrange
    name, _ = _registered
    runner = CliRunner()
    # Act
    result = runner.invoke(archive_claude_bloat, [name])
    # Assert
    assert result.exit_code == 0


def test_cli_prints_archived_summary_line(_registered):
    # Arrange
    name, _ = _registered
    runner = CliRunner()
    # Act
    result = runner.invoke(archive_claude_bloat, [name])
    # Assert — single-line summary format locked by the doctrine.
    assert "archived: " in result.output


def test_cli_summary_includes_file_count(_registered):
    # Arrange
    name, _ = _registered
    runner = CliRunner()
    # Act
    result = runner.invoke(archive_claude_bloat, [name])
    # Assert
    assert "1,500 files" in result.output


def test_cli_unknown_agent_exits_nonzero(tmp_path: Path, env_save_restore):
    # Arrange — clean HOME with no registered agents.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    runner = CliRunner()
    # Act
    result = runner.invoke(archive_claude_bloat, ["nope-not-registered"])
    # Assert
    assert result.exit_code == 2


def test_cli_no_bloat_emits_no_op_message(tmp_path: Path, env_save_restore):
    # Arrange — registered agent with a healthy small .claude/ tree.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".claude").mkdir()
    _populate_subdir(workdir / ".claude", "hooks", 5)

    name = "healthy-target"
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    spec_path = agents_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": explicit_spec(
                    {
                        "runtime": "apptainer",
                        "host": "${HOSTNAME}",
                        "workdir": str(workdir),
                        "apptainer": {"image": "/x.sif", "binds": []},
                        "claude": {"model": "sonnet"},
                        "health": {"enabled": True, "interval": 60},
                        "restart": {"policy": "on-failure", "max_retries": 3},
                    }
                ),
            }
        )
    )

    from scitex_agent_container._state.registry import Registry

    Registry().add(name=name, config_path=str(spec_path), screen_name=name)
    runner = CliRunner()
    # Act
    result = runner.invoke(archive_claude_bloat, [name])
    # Assert
    assert "nothing to archive" in result.output


def test_cli_does_not_delete_original_source(_registered):
    # Arrange — bloat tree exists pre-invocation; archive must MOVE,
    # never delete. The invariant is that every file from the original
    # tree is reachable AFTER the run, at the archive destination.
    name, workdir = _registered
    runner = CliRunner()
    # Act
    runner.invoke(archive_claude_bloat, [name])
    archive_root = next((workdir / ".claude").glob(".archived-*"))
    moved_dir = archive_root / "hooks" / "pre-tool-use" / ".pending"
    # Assert — moved tree carries the full original file count, proving
    # move-not-delete semantics.
    assert len(list(moved_dir.iterdir())) == 1_500
