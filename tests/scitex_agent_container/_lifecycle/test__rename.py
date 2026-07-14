"""The rename engine: preflight, plan, apply — and the rollback, at EVERY step.

A rollback that has never been exercised does not work. So the rollback
here is not one happy test: a failure is injected at each of the eight
steps in turn (the ``rolled_back`` fixture is parametrised over all of
them), and every one must leave the agent EXACTLY as it was — spec text,
directory contents, state.db rows, and card ownership. One organic failure
(a read-only overlays dir, no injection at all) covers the case where the
world, not a callback, says no.

Fully isolated: an injected ``Layout`` root and an explicit tmp board.
Nothing here can see the live fleet — ``test__rename_isolation.py`` proves
that separately, and proves it rather than assuming it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._rename import (
    STEPS,
    agent_rename,
    apply_plan,
)
from scitex_agent_container._lifecycle._rename_cards import find_owned_cards
from scitex_agent_container._lifecycle._rename_plan import (
    Layout,
    RenameError,
    build_plan,
    probe_running,
)

from .._helpers.fleet_root import (
    isolated_board,
    make_fleet,
    make_spec,
    make_state_db,
    seed_cards,
)

OLD = "scitex-todo"
NEW = "scitex-cards"

# A pid that is essentially never allocated — stands in for a crashed
# agent's stale pid file.
DEAD_PID = "2147483646"


class Boom(RuntimeError):
    """An injected mid-rename failure."""


@dataclass
class World:
    """The whole isolated world a rename touches, plus a way to photograph it."""

    layout: Layout
    board: Path
    cards: list[str]
    before: dict = field(default_factory=dict)
    error: str = ""

    def snapshot(self) -> dict:
        """Everything that must be identical after a rolled-back rename."""
        return {
            "spec_text": _read(self.layout.spec_file(OLD)),
            "overlay_marker": _read(
                self.layout.overlay_dir(OLD) / "upper" / "home" / "agent" / "marker"
            ),
            "runtime_marker": _read(self.layout.runtime_dir(OLD) / "session.jsonl"),
            "registry_text": _read(self.layout.registry_json(OLD)),
            "new_side_exists": any(
                p.exists()
                for p in (
                    self.layout.spec_dir(NEW),
                    self.layout.overlay_dir(NEW),
                    self.layout.runtime_dir(NEW),
                    self.layout.registry_json(NEW),
                )
            ),
            "db_names": _db_names(self.layout.state_db),
            "cards_owned_by_old": sorted(find_owned_cards(OLD, store=self.board)),
            "cards_owned_by_new": sorted(find_owned_cards(NEW, store=self.board)),
        }


def _read(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


def _db_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        nodes = conn.execute("SELECT name FROM comms_nodes").fetchall()
        turns = conn.execute("SELECT name FROM turns").fetchall()
    finally:
        conn.close()
    return sorted([r[0] for r in nodes] + [t[0] for t in turns])


def _raise_at(step_to_fail: str):
    """An ``on_step`` callback that aborts the rename at one step.

    Not a mock: ``on_step`` is the REAL progress hook the CLI passes on
    every run. A test uses that same seam to simulate the interruption
    (crash, Ctrl-C, ENOSPC) the rollback exists for.
    """

    def _on_step(step: str) -> None:
        if step == step_to_fail:
            raise Boom(f"injected failure at {step}")

    return _on_step


def _refusal(world: World, old: str = OLD, new: str = NEW) -> str:
    """Run a plan that must be refused; return the refusal message."""
    try:
        build_plan(old, new, layout=world.layout, store=world.board)
    except RenameError as exc:
        return str(exc)
    raise AssertionError(f"build_plan({old!r} -> {new!r}) was NOT refused")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def world(tmp_path: Path, board: Path) -> World:
    """A complete isolated fleet: agent on disk, rows in state.db, cards on the board."""
    layout = make_fleet(tmp_path / "fleet", OLD)
    db = make_state_db(layout)
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO comms_nodes (name, host, a2a_port, registered_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            (OLD, "h", 9001, 1.0, 1.0),
        )
        conn.execute(
            "INSERT INTO turns (turn_id, name, host, status, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t1", OLD, "h", "ok", 1.0),
        )
    conn.close()
    # Two cards, not twenty: every card write goes through the REAL
    # scitex-todo store, whose per-write cost is ~2.2 s of uncached
    # importlib entry-point rescanning (see _helpers.fleet_root.add_card).
    # Two is enough to prove "every card follows"; the card behaviour itself
    # is covered exhaustively in test__rename_cards.py.
    cards = seed_cards(board, OLD, 2)
    built = World(layout=layout, board=board, cards=cards)
    built.before = built.snapshot()
    return built


@pytest.fixture
def renamed(world: World) -> World:
    """The happy path, already applied."""
    agent_rename(OLD, NEW, layout=world.layout, store=world.board)
    return world


@pytest.fixture(params=STEPS, ids=list(STEPS))
def rolled_back(world: World, request) -> World:
    """A rename that FAILED at ``request.param`` and rolled itself back.

    Parametrised over every step, so each assertion below is checked at
    eight distinct points of failure.
    """
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    try:
        apply_plan(plan, store=world.board, on_step=_raise_at(request.param))
    except RenameError as exc:
        world.error = str(exc)
        return world
    raise AssertionError(f"apply_plan did not fail at step {request.param!r}")


@pytest.fixture
def organic_failure(world: World) -> World:
    """A rename that failed because the WORLD said no — no injected callback.

    The overlays parent is read-only, so ``shutil.move`` genuinely cannot
    place the overlay. Steps 1-2 have already run and must be undone.
    """
    overlays = world.layout.overlay_dir(OLD).parent
    overlays.chmod(0o555)
    try:
        plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
        try:
            apply_plan(plan, store=world.board)
        except RenameError as exc:
            world.error = str(exc)
            return world
        raise AssertionError("the read-only overlays dir did NOT fail the rename")
    finally:
        overlays.chmod(0o755)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_refuses_an_unknown_agent(world: World):
    # Arrange
    unknown = "no-such-agent"
    # Act
    message = _refusal(world, old=unknown)
    # Assert
    assert "not found" in message


def test_preflight_refuses_a_name_that_already_exists(world: World):
    # Arrange
    make_fleet(world.layout.root, NEW)
    # Act
    message = _refusal(world)
    # Assert
    assert "already exists" in message


def test_preflight_refuses_an_invalid_new_name(world: World):
    # Arrange
    bad = "Has/Slash"
    # Act
    message = _refusal(world, new=bad)
    # Assert
    assert "invalid agent name" in message


def test_preflight_refuses_a_running_agent(world: World):
    """The pid file names a LIVE process — this very one."""
    # Arrange
    (world.layout.runtime_dir(OLD) / "pid").write_text(f"{os.getpid()}\n")
    # Act
    message = _refusal(world)
    # Assert
    assert "is running" in message


def test_the_running_refusal_names_the_command_to_stop_it(world: World):
    """A refusal without a remedy is just an obstacle."""
    # Arrange
    (world.layout.runtime_dir(OLD) / "pid").write_text(f"{os.getpid()}\n")
    # Act
    message = _refusal(world)
    # Assert
    assert f"sac agents stop {OLD}" in message


def test_preflight_refuses_when_liveness_is_unknown(world: World):
    """A corrupt pid file proves nothing, and 'unknown' must never proceed."""
    # Arrange
    (world.layout.runtime_dir(OLD) / "pid").write_text("not-a-pid\n")
    # Act
    message = _refusal(world)
    # Assert
    assert "is unknown" in message


def test_a_stale_pid_file_does_not_block_the_rename(world: World):
    """A DEAD pid is not evidence of running — else one crash wedges the agent."""
    # Arrange
    (world.layout.runtime_dir(OLD) / "pid").write_text(f"{DEAD_PID}\n")
    # Act
    verdict, _reason = probe_running(OLD, world.layout)
    # Assert
    assert verdict == "stopped"


# ---------------------------------------------------------------------------
# Plan — what --dry-run shows, and that it mutates NOTHING
# ---------------------------------------------------------------------------


def test_the_plan_counts_every_card_that_would_be_reassigned(world: World):
    # Arrange
    expected = set(world.cards)
    # Act
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Assert
    assert set(plan.card_ids) == expected


def test_the_plan_lists_the_board_identity_among_the_spec_changes(world: World):
    # Arrange
    needle = "SCITEX_TODO_AGENT_ID"
    # Act
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Assert
    assert any(needle in c.path for c in plan.spec_changes)


def test_the_plan_counts_the_state_db_rows(world: World):
    # Arrange
    key = "comms_nodes.name"
    # Act
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Assert
    assert plan.db_counts[key] == 1


def test_building_a_plan_changes_nothing(world: World):
    """--dry-run must be exactly that."""
    # Arrange
    before = world.before
    # Act
    build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Assert
    assert world.snapshot() == before


def test_the_plan_warns_when_the_workdir_target_does_not_exist(world: World):
    """sac renames the AGENT, not the repo — say so before the next start fails."""
    # Arrange
    needle = "sac renames the AGENT, not the repo"
    # Act
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Assert
    assert any(needle in w for w in plan.warnings)


def test_no_cards_mode_warns_that_the_cards_will_be_orphaned(world: World):
    # Arrange
    needle = "ORPHANED"
    # Act
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board, cards=False)
    # Assert
    assert any(needle in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# Apply — the happy path
# ---------------------------------------------------------------------------


def test_rename_moves_the_spec_dir(renamed: World):
    # Arrange
    spec = renamed.layout.spec_file(NEW)
    # Act
    exists = spec.is_file()
    # Assert
    assert exists


def test_rename_removes_the_old_spec_dir(renamed: World):
    # Arrange
    old_dir = renamed.layout.spec_dir(OLD)
    # Act
    exists = old_dir.exists()
    # Assert
    assert not exists


def test_rename_rewrites_the_board_identity_in_the_spec(renamed: World):
    # Arrange
    expected = f"SCITEX_TODO_AGENT_ID={NEW}"
    # Act
    text = renamed.layout.spec_file(NEW).read_text()
    # Assert
    assert expected in text


def test_rename_rewrites_the_state_db_path_in_the_spec(renamed: World):
    # Arrange
    expected = f"SCITEX_AGENT_CONTAINER_STATE_DB=/state/{NEW}/state.db"
    # Act
    text = renamed.layout.spec_file(NEW).read_text()
    # Assert
    assert expected in text


def test_rename_moves_the_overlay_dir_with_its_contents(renamed: World):
    # Arrange
    marker = renamed.layout.overlay_dir(NEW) / "upper" / "home" / "agent" / "marker"
    # Act
    content = marker.read_text()
    # Assert
    assert content == OLD  # the file MOVED; its bytes are untouched


def test_rename_moves_the_runtime_dir(renamed: World):
    # Arrange
    session = renamed.layout.runtime_dir(NEW) / "session.jsonl"
    # Act
    exists = session.is_file()
    # Assert
    assert exists


def test_rename_repoints_the_registry_entry_name(renamed: World):
    # Arrange
    path = renamed.layout.registry_json(NEW)
    # Act
    entry = json.loads(path.read_text())
    # Assert
    assert entry["name"] == NEW


def test_rename_repoints_the_registry_config_path(renamed: World):
    # Arrange
    expected = str(renamed.layout.spec_file(NEW))
    # Act
    entry = json.loads(renamed.layout.registry_json(NEW).read_text())
    # Assert
    assert entry["config"] == expected


def test_rename_moves_the_state_db_rows(renamed: World):
    # Arrange
    expected = [NEW, NEW]
    # Act
    names = _db_names(renamed.layout.state_db)
    # Assert
    assert names == expected


def test_rename_leaves_no_card_orphaned(renamed: World):
    """THE point of the verb."""
    # Arrange
    store = renamed.board
    # Act
    orphans = find_owned_cards(OLD, store=store)
    # Assert
    assert orphans == []


def test_rename_gives_every_card_to_the_new_owner(renamed: World):
    # Arrange
    expected = set(renamed.cards)
    # Act
    owned = set(find_owned_cards(NEW, store=renamed.board))
    # Assert
    assert owned == expected


def test_rename_keeps_the_operators_spec_comments(renamed: World):
    # Arrange
    marker = "# This comment block is LOAD-BEARING"
    # Act
    text = renamed.layout.spec_file(NEW).read_text()
    # Assert
    assert marker in text


def test_a_renamed_agent_can_be_renamed_back(world: World):
    """The rename is not a one-way door."""
    # Arrange
    agent_rename(OLD, NEW, layout=world.layout, store=world.board)
    # Act
    agent_rename(NEW, OLD, layout=world.layout, store=world.board)
    # Assert
    assert world.layout.spec_file(OLD).read_text() == make_spec(OLD)


# ---------------------------------------------------------------------------
# ROLLBACK — injected at EVERY step (the fixture is parametrised over STEPS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failing_step", STEPS)
def test_a_failure_at_any_step_raises_rename_error(world: World, failing_step: str):
    # Arrange
    plan = build_plan(OLD, NEW, layout=world.layout, store=world.board)
    # Act
    # Assert
    with pytest.raises(RenameError):
        apply_plan(plan, store=world.board, on_step=_raise_at(failing_step))


def test_rollback_leaves_the_world_exactly_as_it_was(rolled_back: World):
    """The only assertion that really matters: NOTHING moved."""
    # Arrange
    world = rolled_back
    # Act
    after = world.snapshot()
    # Assert
    assert after == world.before


def test_rollback_leaves_nothing_under_the_new_name(rolled_back: World):
    # Arrange
    new_spec_dir = rolled_back.layout.spec_dir(NEW)
    # Act
    exists = new_spec_dir.exists()
    # Assert
    assert not exists


def test_rollback_hands_every_card_back(rolled_back: World):
    """A `verify`-step failure happens AFTER the cards moved — they must return."""
    # Arrange
    expected = set(rolled_back.cards)
    # Act
    owned = set(find_owned_cards(OLD, store=rolled_back.board))
    # Assert
    assert owned == expected


def test_rollback_leaves_the_new_owner_holding_no_cards(rolled_back: World):
    # Arrange
    store = rolled_back.board
    # Act
    owned = find_owned_cards(NEW, store=store)
    # Assert
    assert owned == []


def test_rollback_restores_the_state_db_rows(rolled_back: World):
    # Arrange
    expected = [OLD, OLD]
    # Act
    names = _db_names(rolled_back.layout.state_db)
    # Assert
    assert names == expected


def test_rollback_restores_the_original_spec_text(rolled_back: World):
    # Arrange
    expected = make_spec(OLD)
    # Act
    text = rolled_back.layout.spec_file(OLD).read_text()
    # Assert
    assert text == expected


def test_rollback_restores_the_overlay_dir(rolled_back: World):
    # Arrange
    marker = (
        rolled_back.layout.overlay_dir(OLD) / "upper" / "home" / "agent" / "marker"
    )
    # Act
    exists = marker.is_file()
    # Assert
    assert exists


def test_the_rollback_message_says_it_rolled_back(rolled_back: World):
    # Arrange
    needle = "Rolled back"
    # Act
    message = rolled_back.error
    # Assert
    assert needle in message


# ---------------------------------------------------------------------------
# ROLLBACK from an ORGANIC failure (no injected callback at all)
# ---------------------------------------------------------------------------


def test_an_organic_move_failure_rolls_the_rename_back(organic_failure: World):
    """The world said no: the overlays dir is read-only. Everything must revert."""
    # Arrange
    world = organic_failure
    # Act
    after = world.snapshot()
    # Assert
    assert after == world.before


def test_an_organic_move_failure_restores_the_spec_dir(organic_failure: World):
    # Arrange
    spec = organic_failure.layout.spec_file(OLD)
    # Act
    exists = spec.is_file()
    # Assert
    assert exists
