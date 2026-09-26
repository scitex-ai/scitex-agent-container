"""The PLAN half of the ``spec.engines`` sweep — selection and bucketing.

Real spec files under ``tmp_path``, planned by the real functions. There was
no test module for this file at all, which is how ``--host`` came to drop the
specs it could not read and ``--limit`` came to re-select the same first N on
every run: both are selection behaviour, and selection was only ever exercised
through the CLI's happy path.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_floor import EngineFloor
from scitex_agent_container._maintenance._engines_migration import (
    STATE_HELD_BACK,
    STATE_MIGRATED,
    STATE_REFUSED,
    STATE_UNREADABLE,
    plan_engines_migration,
    read_spec_text,
    select_spec_paths,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

#: These tests are about SELECTION, BATCHING and LINE ENDINGS, not about
#: host capability. `floor` is a required argument now — omitting it used to
#: disable the floor silently — so saying "no floor" is a value, not a gap.
_NO_FLOOR = EngineFloor.disabled()


def _write_spec(root: Path, name: str, *, model="opus[1m]", **overrides) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    spec = {"to_home": "./to_home", "claude": {"model": model}}
    spec.update(overrides)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(explicit_doc(spec), sort_keys=False))
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir()
    return agents


def _states(plan) -> "dict[str, str]":
    return {o.agent: o.state for o in plan.outcomes}


# ---------------------------------------------------------------------------
# --host must not make a spec vanish
# ---------------------------------------------------------------------------


def test_the_host_filter_selects_the_named_hosts_specs(root: Path) -> None:
    # Arrange
    _write_spec(root, "here", host="scitex-compute-04")
    _write_spec(root, "there", host="scitex-compute-01")
    # Act
    paths, _ = select_spec_paths(root, hosts=("scitex-compute-04",))
    # Assert
    assert [p.parent.name for p in paths] == ["here"]


def test_the_host_filter_keeps_an_unparsable_spec(root: Path) -> None:
    # Arrange — it cannot be ruled out, so it must reach the plan by name.
    _write_spec(root, "here", host="scitex-compute-04")
    broken = _write_spec(root, "broken", host="scitex-compute-04")
    broken.write_text(broken.read_text() + "  bad: [unclosed\n")
    # Act
    paths, _ = select_spec_paths(root, hosts=("scitex-compute-04",))
    # Assert
    assert "broken" in [p.parent.name for p in paths]


def test_the_host_filter_keeps_a_spec_it_cannot_open(root: Path) -> None:
    # Arrange
    _write_spec(root, "here", host="scitex-compute-04")
    broken = _write_spec(root, "broken", host="scitex-compute-04")
    broken.chmod(0o000)
    # Act
    try:
        paths, _ = select_spec_paths(root, hosts=("scitex-compute-04",))
    finally:
        broken.chmod(0o644)
    # Assert
    assert "broken" in [p.parent.name for p in paths]


def test_an_unopenable_spec_reaches_the_plan_as_unreadable(root: Path) -> None:
    # Arrange
    broken = _write_spec(root, "broken", host="scitex-compute-04")
    broken.chmod(0o000)
    # Act
    try:
        paths, _ = select_spec_paths(root, hosts=("scitex-compute-04",))
        plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR)
    finally:
        broken.chmod(0o644)
    # Assert
    assert _states(plan) == {"broken": STATE_UNREADABLE}


def test_an_unreadable_spec_makes_the_plan_unsafe(root: Path) -> None:
    # Arrange — this is the guard --host used to disable.
    broken = _write_spec(root, "broken", host="scitex-compute-04")
    broken.chmod(0o000)
    # Act
    try:
        paths, _ = select_spec_paths(root, hosts=("scitex-compute-04",))
        plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR)
    finally:
        broken.chmod(0o644)
    # Assert
    assert plan.safe_to_apply is False


def test_a_spec_placed_on_another_host_is_still_excluded(root: Path) -> None:
    # Arrange — the filter must still filter; keeping the unreadable ones is
    # not "keep everything".
    _write_spec(root, "here", host="scitex-compute-04")
    _write_spec(root, "there", host="scitex-compute-01")
    # Act
    paths, _ = select_spec_paths(root, hosts=("scitex-compute-01",))
    # Assert
    assert [p.parent.name for p in paths] == ["there"]


# ---------------------------------------------------------------------------
# --limit caps what is WRITTEN, so a batch can advance
# ---------------------------------------------------------------------------


def test_the_limit_caps_the_migratable_specs(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2", "b3"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=2)
    # Assert
    assert [o.agent for o in plan.migrated] == ["b1", "b2"]


def test_the_specs_past_the_limit_are_held_back_not_dropped(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2", "b3"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=2)
    # Assert
    assert [o.agent for o in plan.held_back] == ["b3"]


def test_a_refused_spec_does_not_consume_the_limit(root: Path) -> None:
    # Arrange — a permanently-refused spec that ate the budget would stall
    # `--limit 1` on it forever.
    _write_spec(root, "b1", model="")
    _write_spec(root, "b2")
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    # Assert
    assert _states(plan) == {"b1": STATE_REFUSED, "b2": STATE_MIGRATED}


def test_an_already_migrated_spec_does_not_consume_the_limit(root: Path) -> None:
    # Arrange — the second batch must reach the specs the first one left.
    first = _write_spec(root, "b1")
    _write_spec(root, "b2")
    paths, _ = select_spec_paths(root)
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    first.write_text(plan.migrated[0].new_text)
    # Act
    again = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    # Assert
    assert [o.agent for o in again.migrated] == ["b2"]


def test_a_held_back_spec_is_never_written(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    # Assert
    assert [o.will_write for o in plan.held_back] == [False]


def test_a_held_back_spec_keeps_the_plan_from_claiming_completion(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    # Assert
    assert plan.is_complete is False


def test_a_refusal_keeps_the_plan_from_claiming_completion(root: Path) -> None:
    # Arrange — a refusal is not a failure, and it is not a finished sweep.
    _write_spec(root, "b1", model="")
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR)
    # Assert
    assert plan.is_complete is False


def test_a_fully_migrated_fleet_is_reported_complete(root: Path) -> None:
    # Arrange — the positive control for the claim above.
    spec = _write_spec(root, "b1")
    paths, _ = select_spec_paths(root)
    spec.write_text(plan_engines_migration(paths, root=root, floor=_NO_FLOOR).migrated[0].new_text)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR)
    # Assert
    assert plan.is_complete is True


def test_a_zero_limit_is_refused(root: Path) -> None:
    # Arrange
    _write_spec(root, "b1")
    paths, _ = select_spec_paths(root)
    # Act — a slice would have accepted it and planned nothing.
    act = lambda: plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=0)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        act()


def test_a_negative_limit_is_refused(root: Path) -> None:
    # Arrange — `picked[:-1]` silently dropped the LAST spec instead.
    for name in ("b1", "b2", "b3"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    act = lambda: plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=-1)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        act()


def test_the_summary_names_what_the_limit_held_back(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    summary = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1).summary()
    # Assert
    assert "held back by --limit" in summary


def test_a_held_back_outcome_is_its_own_state(root: Path) -> None:
    # Arrange
    for name in ("b1", "b2"):
        _write_spec(root, name)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR, limit=1)
    # Assert
    assert _states(plan) == {"b1": STATE_MIGRATED, "b2": STATE_HELD_BACK}


# ---------------------------------------------------------------------------
# Reading a spec must not silently rewrite it
# ---------------------------------------------------------------------------


def test_read_spec_text_keeps_crlf_endings(tmp_path: Path) -> None:
    # Arrange — Path.read_text translates them away, which makes
    # `_yaml_line_edit.split_ending`'s CRLF handling unreachable and rewrites
    # every line of the file.
    path = tmp_path / "spec.yaml"
    path.write_bytes(b"spec:\r\n  claude:\r\n")
    # Act
    text = read_spec_text(path)
    # Assert
    assert text == "spec:\r\n  claude:\r\n"


def test_a_crlf_spec_is_planned_with_crlf_endings(root: Path) -> None:
    # Arrange
    spec = _write_spec(root, "alpha")
    spec.write_bytes(spec.read_text().replace("\n", "\r\n").encode())
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=_NO_FLOOR)
    # Assert
    assert "\r\n" in plan.migrated[0].new_text


def test_a_crlf_spec_gains_no_bare_line_feed(root: Path) -> None:
    # Arrange
    spec = _write_spec(root, "alpha")
    spec.write_bytes(spec.read_text().replace("\n", "\r\n").encode())
    paths, _ = select_spec_paths(root)
    # Act
    new_text = plan_engines_migration(paths, root=root, floor=_NO_FLOOR).migrated[0].new_text
    # Assert
    assert new_text.count("\n") == new_text.count("\r\n")
