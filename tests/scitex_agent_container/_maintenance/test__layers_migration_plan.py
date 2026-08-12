"""Planning the ``to_home_layers`` sweep over a real corpus of real spec files.

The plan is what an operator reads before letting anything write to 100+
hand-maintained files, so the properties under test are the ones that decide
whether that reading is trustworthy:

* the declaration written is the cascade the spec ALREADY resolves (the entire
  zero-behaviour-change argument), and
* the three not-written outcomes stay apart — a REFUSAL (a shape the editor
  declines), an UNREADABLE spec (never reached the editor), and an
  ALREADY-DECLARED spec (finished) mean completely different things, and
  collapsing any two lets a sweep report "done" over specs it never touched.

STX-NM002: no mocks — real spec files on disk, loaded by the real loader,
resolved by the real resolver. The cascade roots are pinned to tmp_path via
the two documented env seams so no test can read the operator's fleet.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._layers_migration_plan import (
    already_declared,
    plan_migration,
    plan_spec,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_SETTINGS = {"hooks": {"PreToolUse": [{"hooks": [{"command": "guard.sh"}]}]}}


def _write_settings(to_home: Path) -> None:
    claude = to_home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(_SETTINGS))


def _write_spec(agents_dir: Path, name: str, *, flow: bool = False, **overrides):
    """A real, loadable v3 spec with a real per-agent ``to_home/`` beside it.

    ``flow`` dumps the document in FLOW style. That spec loads exactly like any
    other, but no line in it BEGINS with ``to_home:``, so the line-anchored
    editor has nothing to anchor to — a genuine refusal, reachable without
    hand-crafting a file the loader would reject for some unrelated reason.
    """
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True)
    _write_settings(agent_dir / "to_home")
    doc = explicit_doc({"to_home": "./to_home", **overrides})
    (agent_dir / "spec.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False, default_flow_style=flow)
    )
    return agent_dir / "spec.yaml"


@pytest.fixture
def fleet(tmp_path: Path):
    """A tmp fleet whose whole cascade is pinned inside tmp_path.

    Both baselines are given DISTINCT directories on purpose: where they point
    at the same place the resolver collapses one as a duplicate, which is what
    happens on the live host and would hide any bug in the multi-layer path.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_settings(agents_dir / "_shared" / "to_home")
    user_shared = tmp_path / "user-baseline" / "to_home"
    _write_settings(user_shared)

    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(agents_dir),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SAC_USER_TO_HOME_BASELINE": str(user_shared),
        "SAC_SPEC_CACHE_DISABLE": "1",
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield agents_dir
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# The writable path — and what it writes
# ---------------------------------------------------------------------------


def test_a_normal_spec_is_planned_as_writable(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.will_write is True


def test_a_planned_edit_adds_exactly_one_line(fleet: Path) -> None:
    # Arrange — anything else is a DEFECT in the editor, not a bigger success.
    spec = _write_spec(fleet, "alpha")
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.lines_added == 1


def test_the_declaration_names_the_layers_that_resolve_today(fleet: Path) -> None:
    # Arrange — all three cascade layers exist and are distinct in this fixture.
    spec = _write_spec(fleet, "alpha")
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.layers == ("user-shared", "project-shared", "per-agent")


def test_a_spec_without_its_own_to_home_dir_omits_that_layer(fleet: Path) -> None:
    # Arrange — a layer that contributes nothing must not be declared, or the
    # spec would claim an inheritance the agent does not have.
    agent_dir = fleet / "beta"
    agent_dir.mkdir()
    doc = explicit_doc({"to_home": "./to_home"})
    spec = agent_dir / "spec.yaml"
    spec.write_text(yaml.safe_dump(doc, sort_keys=False))
    # Act
    edit = plan_spec(spec)
    # Assert
    assert "per-agent" not in edit.layers


def test_the_new_text_contains_the_rendered_declaration(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "alpha")
    # Act
    edit = plan_spec(spec)
    # Assert
    assert "to_home_layers: [user-shared, project-shared, per-agent]" in edit.new_text


# ---------------------------------------------------------------------------
# Refused — expected, named, and NOT a failure
# ---------------------------------------------------------------------------


def test_a_spec_with_no_anchor_line_is_refused(fleet: Path) -> None:
    # Arrange — flow style: loads fine, but no line starts with `to_home:`.
    spec = _write_spec(fleet, "flowed", flow=True)
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.refusal is not None


def test_a_refused_spec_is_not_written(fleet: Path) -> None:
    # Arrange
    spec = _write_spec(fleet, "flowed", flow=True)
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.will_write is False


def test_a_refusal_alone_leaves_the_plan_safe_to_apply(fleet: Path) -> None:
    # Arrange — a named, counted refusal is a legitimate outcome a human
    # resolves; it must not block the other 100 specs.
    _write_spec(fleet, "alpha")
    _write_spec(fleet, "flowed", flow=True)
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert plan.safe_to_apply is True


# ---------------------------------------------------------------------------
# Unreadable — a different thing from refused, and it DOES block
# ---------------------------------------------------------------------------


def test_an_unloadable_spec_is_reported_as_unreadable(fleet: Path) -> None:
    # Arrange
    agent_dir = fleet / "broken"
    agent_dir.mkdir()
    (agent_dir / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert len(plan.unreadable) == 1


def test_an_unreadable_spec_makes_the_plan_unsafe(fleet: Path) -> None:
    # Arrange — a plan that cannot describe every spec does not describe the
    # sweep, so it must not be applied.
    _write_spec(fleet, "alpha")
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert plan.safe_to_apply is False


def test_an_unreadable_spec_is_not_counted_as_refused(fleet: Path) -> None:
    # Arrange — a refusal is the editor declining a shape it knows; this never
    # reached the editor, and conflating them hides a real blocker.
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert plan.refused == ()


def test_the_unreadable_reason_names_the_agent(fleet: Path) -> None:
    # Arrange
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert plan.unreadable[0].startswith("broken: ")


def test_the_unreadable_reason_survives_a_multiline_error(fleet: Path) -> None:
    # Arrange — the validator puts "Config validation failed for <path>:" on
    # line one and WHAT is wrong on the lines after, so a first-line-only
    # reason reports that something is wrong while discarding what.
    broken = fleet / "broken"
    broken.mkdir()
    (broken / "spec.yaml").write_text("this: is not an agent spec\n")
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert "Unknown top-level field 'this'" in plan.unreadable[0]


# ---------------------------------------------------------------------------
# Already declared — the third not-written outcome, and the one that is done
# ---------------------------------------------------------------------------


def test_an_already_declared_spec_is_not_written_again(fleet: Path) -> None:
    # Arrange — re-running the sweep must not duplicate the key.
    spec = _write_spec(fleet, "declared", to_home_layers=["user-shared"])
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.will_write is False


def test_an_already_declared_spec_is_not_a_refusal(fleet: Path) -> None:
    # Arrange — a completed re-run must not read as a fleet needing attention.
    spec = _write_spec(fleet, "declared", to_home_layers=["user-shared"])
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.refusal is None


def test_an_already_declared_spec_lands_in_its_own_bucket(fleet: Path) -> None:
    # Arrange
    _write_spec(fleet, "declared", to_home_layers=["user-shared"])
    # Act
    plan = plan_migration(sorted(fleet.glob("*/spec.yaml")))
    # Assert
    assert [e.agent for e in already_declared(plan)] == ["declared"]


def test_a_declared_spec_reports_only_the_layers_it_declared(fleet: Path) -> None:
    # Arrange — the resolver honours the declaration, so the plan must too.
    spec = _write_spec(fleet, "declared", to_home_layers=["user-shared"])
    # Act
    edit = plan_spec(spec)
    # Assert
    assert edit.layers == ("user-shared",)
