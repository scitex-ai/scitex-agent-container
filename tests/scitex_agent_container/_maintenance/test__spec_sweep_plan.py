"""Tests for the shared spec-sweep plan builder.

The distinction these exist to protect is the model's own: a REFUSAL is the
editor declining a shape it does not know (correct behaviour, does not block
the sweep), while MALFORMED and UNREADABLE mean the plan does not describe what
would happen (and must block it).

STX-NM002: no mocks, no monkeypatch — real files in a tmp_path, real editor.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._maintenance._spec_sweep_plan import (
    fleet_spec_paths,
    group_refusals,
    plan_spec_sweep,
)
from scitex_agent_container.config._a2a_host_line import insert_a2a_host

_WITHOUT_HOST = "spec:\n  host: ywata-note-win\n  a2a:\n    port: auto\n"
_WITH_HOST = "spec:\n  a2a:\n    port: auto\n    host: 127.0.0.1\n"
_NO_A2A = "spec:\n  runtime: tui\n"


def _write(root, agent: str, text: str):
    d = root / agent
    d.mkdir(parents=True, exist_ok=True)
    path = d / "spec.yaml"
    path.write_text(text)
    return path


@pytest.fixture()
def fleet(tmp_path):
    """A miniature fleet: one to migrate, one already done, one unrecognised."""
    root = tmp_path / "agents"
    root.mkdir()
    _write(root, "needs-it", _WITHOUT_HOST)
    _write(root, "already-has-it", _WITH_HOST)
    _write(root, "no-a2a-block", _NO_A2A)
    _write(root, "_template_thing", _WITHOUT_HOST)
    yield root


def test_only_the_spec_missing_the_key_would_be_written(fleet) -> None:
    # Arrange
    root = fleet
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert [e.agent for e in plan.writable] == ["needs-it"]


def test_scaffolding_directories_are_not_treated_as_agents(fleet) -> None:
    # Arrange — `_template_*` and `_shared` are not fleet agents.
    root = fleet
    # Act
    paths = fleet_spec_paths(root)
    # Assert
    assert all(not p.parent.name.startswith("_") for p in paths)


def test_a_planned_write_adds_exactly_one_line(fleet) -> None:
    # Arrange
    root = fleet
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert plan.writable[0].lines_added == 1


def test_an_unrecognised_shape_is_refused_and_named(fleet) -> None:
    # Arrange — silently skipping is how a sweep reports "2 done" over 3.
    root = fleet
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert "no-a2a-block" in {e.agent for e in plan.refused}


def test_refusals_do_not_make_the_plan_unsafe(fleet) -> None:
    # Arrange — a named, counted refusal is a legitimate outcome.
    root = fleet
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert plan.safe_to_apply is True


def test_nothing_is_written_by_planning(fleet) -> None:
    # Arrange
    root = fleet
    before = (root / "needs-it" / "spec.yaml").read_text()
    # Act
    plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert (root / "needs-it" / "spec.yaml").read_text() == before


def test_an_unreadable_spec_makes_the_plan_unsafe(tmp_path) -> None:
    # Arrange — undecodable bytes: we never got to LOOK, so the plan does not
    # describe what would happen. Different from a refusal, and fatal.
    root = tmp_path / "agents"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "spec.yaml").write_bytes(b"\xff\xfe\x00binary")
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert plan.safe_to_apply is False


def test_an_unreadable_spec_is_not_counted_as_a_refusal(tmp_path) -> None:
    # Arrange
    root = tmp_path / "agents"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "spec.yaml").write_bytes(b"\xff\xfe\x00binary")
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert plan.refused == ()


def test_refusals_are_grouped_by_reason(fleet) -> None:
    # Arrange — with 101 already-declaring specs, a flat list of agent names
    # buries the one refusal that needs a human.
    root = fleet
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Act
    grouped = group_refusals(plan)
    # Assert
    assert grouped["already declares spec.a2a.host"] == ("already-has-it",)


def test_grouping_separates_the_benign_from_the_unrecognised(fleet) -> None:
    # Arrange
    root = fleet
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Act
    grouped = group_refusals(plan)
    # Assert
    assert grouped["no spec.a2a block to anchor to"] == ("no-a2a-block",)


def test_an_empty_registry_plans_nothing(tmp_path) -> None:
    # Arrange
    root = tmp_path / "agents"
    root.mkdir()
    # Act
    plan = plan_spec_sweep(root, insert_a2a_host)
    # Assert
    assert plan.edits == ()
