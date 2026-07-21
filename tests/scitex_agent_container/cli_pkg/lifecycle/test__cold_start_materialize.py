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

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

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


# ---------------------------------------------------------------------------
# resolve_cold_start_targets — the sac-start orchestration (fs-precedence)
# ---------------------------------------------------------------------------

from scitex_agent_container.cli_pkg.lifecycle._cold_start import (  # noqa: E402
    resolve_cold_start_targets,
)


def test_plain_agent_name_passes_through_unchanged(tmp_path):
    # Arrange
    targets = ["proj-figrecipe"]
    # Act
    rewritten, plans = resolve_cold_start_targets(
        targets, caller_host="h", base_dir=tmp_path / "agents"
    )
    # Assert
    assert rewritten == ["proj-figrecipe"] and plans == []


def test_existing_spec_yaml_path_passes_through(tmp_path):
    # Arrange — an explicit spec.yaml path must NOT be cold-started.
    spec = tmp_path / "foo" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text(explicitize_yaml("apiVersion: scitex-agent-container/v3\nkind: Agent\nspec: {}\n"))
    # Act
    rewritten, plans = resolve_cold_start_targets(
        [str(spec)], caller_host="h", base_dir=tmp_path / "agents"
    )
    # Assert
    assert rewritten == [str(spec)] and plans == []


def test_agents_root_dir_passes_through_for_bulk(tmp_path):
    # Arrange — a dir with <name>/spec.yaml children = existing bulk target.
    root = tmp_path / "agents_root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "spec.yaml").write_text("kind: Agent\n")
    # Act
    rewritten, plans = resolve_cold_start_targets(
        [str(root)], caller_host="h", base_dir=tmp_path / "agents"
    )
    # Assert
    assert rewritten == [str(root)] and plans == []


def test_workdir_path_is_cold_started_to_its_label(tmp_path):
    # Arrange — a plain project workdir (no spec inside) → cold-start.
    work = tmp_path / "myproj"
    work.mkdir()
    (work / "README.md").write_text("x")  # non-empty → a real workdir
    # Act
    rewritten, plans = resolve_cold_start_targets(
        [str(work)], caller_host="h", base_dir=tmp_path / "agents"
    )
    # Assert
    assert rewritten == ["myproj"]


def test_workdir_cold_start_records_a_plan(tmp_path):
    # Arrange
    work = tmp_path / "myproj"
    work.mkdir()
    (work / "README.md").write_text("x")  # non-empty → a real workdir
    # Act
    rewritten, plans = resolve_cold_start_targets(
        [str(work)], caller_host="h", base_dir=tmp_path / "agents"
    )
    # Assert
    assert plans[0].label == "myproj" and plans[0].action == "create"


def test_dry_run_would_create_is_not_added_to_launch_list(tmp_path):
    # Arrange
    work = tmp_path / "myproj"
    work.mkdir()
    (work / "README.md").write_text("x")  # non-empty → a real workdir
    # Act
    rewritten, plans = resolve_cold_start_targets(
        [str(work)], caller_host="h", base_dir=tmp_path / "agents", dry_run=True
    )
    # Assert
    assert rewritten == [] and plans[0].action == "would-create"
