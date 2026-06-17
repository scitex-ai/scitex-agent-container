"""Tests for cold-start spec materialization (operator TODO 2026-06-17).

``materialize_cold_start`` turns a parsed :class:`ColdStartTarget` into a real
``<agents-root>/<label>/{spec.yaml,to_home/}`` (minimal standardized TUI spec)
and returns a plan, so ``sac start`` can then launch it through the existing
flow. Contract:

  * fresh label  → writes a v3-valid TUI spec (action="create").
  * existing label, SAME workdir+host → reuse, no overwrite (action="reuse").
  * existing label, DIFFERENT workdir/host → fail loud unless ``force``.
  * ``dry_run`` → never writes; reports the intended action.

Conventions: one assert / AAA markers; no mocks (real YAML under tmp_path,
exercised through the production ``load_config`` / ``validate_config``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._cold_start import (
    ColdStartConflictError,
    ColdStartTarget,
    materialize_cold_start,
)


def _target(tmp_path: Path, label="figrecipe", host="ywata-note-win"):
    return ColdStartTarget(label=label, host=host, workdir=str(tmp_path / "work"))


def test_fresh_label_writes_spec_yaml(tmp_path):
    # Arrange
    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    # Assert
    assert Path(plan.spec_path).is_file()


def test_fresh_label_action_is_create(tmp_path):
    # Arrange
    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    # Assert
    assert plan.action == "create"


def test_written_spec_is_v3_valid(tmp_path):
    # Arrange
    import scitex_agent_container as sac

    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    errors = sac.validate_config(plan.spec_path)
    # Assert
    assert errors == []


def test_written_spec_is_tui_runtime(tmp_path):
    # Arrange
    from scitex_agent_container.config import load_config

    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    cfg = load_config(plan.spec_path)
    # Assert
    assert cfg.runtime == "tui"


def test_written_spec_has_the_workdir(tmp_path):
    # Arrange
    from scitex_agent_container.config import load_config

    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    cfg = load_config(plan.spec_path)
    # Assert
    assert cfg.workdir == str(tmp_path / "work")


def test_creates_to_home_dir(tmp_path):
    # Arrange
    t = _target(tmp_path)
    # Act
    materialize_cold_start(t, base_dir=tmp_path / "agents")
    # Assert
    assert (tmp_path / "agents" / "figrecipe" / "to_home").is_dir()


def test_existing_label_same_target_is_reused(tmp_path):
    # Arrange — materialize once, then again with the identical target.
    t = _target(tmp_path)
    materialize_cold_start(t, base_dir=tmp_path / "agents")
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents")
    # Assert
    assert plan.action == "reuse"


def test_existing_label_different_workdir_fails_loud(tmp_path):
    # Arrange — same label, different workdir → must not silently clobber.
    materialize_cold_start(_target(tmp_path), base_dir=tmp_path / "agents")
    other = ColdStartTarget(
        label="figrecipe", host="ywata-note-win", workdir=str(tmp_path / "OTHER")
    )
    # Act
    # Assert
    with pytest.raises(ColdStartConflictError):
        materialize_cold_start(other, base_dir=tmp_path / "agents")


def test_existing_label_different_workdir_force_overwrites(tmp_path):
    # Arrange
    materialize_cold_start(_target(tmp_path), base_dir=tmp_path / "agents")
    other = ColdStartTarget(
        label="figrecipe", host="ywata-note-win", workdir=str(tmp_path / "OTHER")
    )
    # Act
    plan = materialize_cold_start(other, base_dir=tmp_path / "agents", force=True)
    # Assert
    assert plan.action == "create"


def test_dry_run_does_not_write_spec(tmp_path):
    # Arrange
    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents", dry_run=True)
    # Assert
    assert not Path(plan.spec_path).exists()


def test_dry_run_reports_would_create_action(tmp_path):
    # Arrange
    t = _target(tmp_path)
    # Act
    plan = materialize_cold_start(t, base_dir=tmp_path / "agents", dry_run=True)
    # Assert
    assert plan.action == "would-create"
