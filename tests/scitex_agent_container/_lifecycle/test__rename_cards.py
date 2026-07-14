"""The cards must follow the agent — this is the whole point of the verb.

Real scitex-todo, real store (a tmp YAML file), real ``reassign_task``.
sac does not touch the board itself, so these tests exercise the PORT: we
put real cards in a real store, run the migration, and read the store back
through scitex-todo's own reader.

The orphaning case is the headline: change the board identity without
migrating and the agent under its new name cannot see its own work, and
nothing says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._rename_cards import (
    find_foreign_scoped_cards,
    find_owned_cards,
    migrate_cards,
    undo_migrate_cards,
)

from .._helpers.fleet_root import add_card, isolated_board, seed_cards

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def owned(board: Path) -> list[str]:
    """Cards owned by OLD, plus one owned by a bystander agent."""
    ids = seed_cards(board, OLD, 3)
    seed_cards(board, "other-agent", 2)
    return ids


@pytest.fixture
def foreign_scoped(board: Path) -> str:
    """A card SCOPED to OLD but OWNED by someone else — inconsistent, and real.

    ``scope`` is supposed to track the owner, but drift happens. Reading
    scope as ownership is the trap: it would make the rename hand this card
    to the new name, taking it from the agent who actually owns it.
    """
    return add_card(
        board, "drifted", owner="other-agent", scope=f"agent:{OLD}"
    )


def _owner_of(store: Path, task_id: str) -> str | None:
    from scitex_todo import _store

    return _store.get_task(store, task_id).get("agent")


def _scope_of(store: Path, task_id: str) -> str | None:
    from scitex_todo import _store

    return _store.get_task(store, task_id).get("scope")


def _assignee_of(store: Path, task_id: str) -> str | None:
    from scitex_todo import _store

    return _store.get_task(store, task_id).get("assignee")


# ---------------------------------------------------------------------------
# find_owned_cards — what --dry-run counts
# ---------------------------------------------------------------------------


def test_find_owned_cards_finds_every_card_the_agent_owns(board: Path, owned: list):
    # Arrange
    expected = set(owned)
    # Act
    found = find_owned_cards(OLD, store=board)
    # Assert
    assert set(found) == expected


def test_find_owned_cards_ignores_another_agents_cards(board: Path, owned: list):
    # Arrange
    stranger_ids = {"other-agent-card-0", "other-agent-card-1"}
    # Act
    found = set(find_owned_cards(OLD, store=board))
    # Assert
    assert not (found & stranger_ids)


def test_find_owned_cards_does_not_claim_a_card_merely_scoped_to_the_agent(
    board: Path, foreign_scoped: str
):
    """SCOPE IS NOT OWNERSHIP — and reading it as ownership steals a card.

    ``reassign_task`` sets ``agent = assignee = scope-owner`` together, so a
    card whose scope says ``agent:<old>`` while its owner says someone else
    is drifted data. Migrating it would take a working agent's card away to
    tidy a string.
    """
    # Arrange
    stolen = foreign_scoped
    # Act
    found = find_owned_cards(OLD, store=board)
    # Assert
    assert stolen not in found


def test_a_foreign_scoped_card_is_reported_rather_than_silently_ignored(
    board: Path, foreign_scoped: str
):
    """Not stealing it is right; saying nothing about it is not."""
    # Arrange
    expected = [foreign_scoped]
    # Act
    reported = find_foreign_scoped_cards(OLD, store=board)
    # Assert
    assert reported == expected


def test_a_card_the_agent_owns_is_never_reported_as_foreign(
    board: Path, owned: list
):
    # Arrange
    store = board
    # Act
    reported = find_foreign_scoped_cards(OLD, store=store)
    # Assert
    assert reported == []


def test_find_owned_cards_returns_empty_for_an_agent_with_no_cards(board: Path):
    # Arrange
    nobody = "agent-with-nothing"
    # Act
    found = find_owned_cards(nobody, store=board)
    # Assert
    assert found == []


# ---------------------------------------------------------------------------
# migrate_cards — THE orphaning case
# ---------------------------------------------------------------------------


def test_migrate_moves_every_card_to_the_new_owner(board: Path, owned: list):
    """Rename with cards present: every card must follow."""
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    owners = {_owner_of(board, task_id) for task_id in owned}
    # Assert
    assert owners == {NEW}


def test_migrate_rescopes_every_card_to_the_new_agent(board: Path, owned: list):
    """``scope`` is the slice the agent lists its work by — it must move too."""
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    scopes = {_scope_of(board, task_id) for task_id in owned}
    # Assert
    assert scopes == {f"agent:{NEW}"}


def test_migrate_moves_the_legacy_assignee_field_in_lockstep(
    board: Path, owned: list
):
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    assignees = {_assignee_of(board, task_id) for task_id in owned}
    # Assert
    assert assignees == {NEW}


def test_migrate_leaves_no_card_owned_by_the_old_name(board: Path, owned: list):
    """The orphan check: after the rename, OLD must own nothing."""
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    orphans = find_owned_cards(OLD, store=board)
    # Assert
    assert orphans == []


def test_migrate_does_not_touch_another_agents_cards(board: Path, owned: list):
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    stranger_owner = _owner_of(board, "other-agent-card-0")
    # Assert
    assert stranger_owner == "other-agent"


def test_migrate_does_not_steal_a_foreign_scoped_card(
    board: Path, foreign_scoped: str
):
    """The card is scoped to OLD but owned by another agent. It stays theirs."""
    # Arrange
    migrate_cards(OLD, NEW, store=board)
    # Act
    owner = _owner_of(board, foreign_scoped)
    # Assert
    assert owner == "other-agent"


def test_migrate_reports_the_cards_it_moved(board: Path, owned: list):
    # Arrange
    expected = set(owned)
    # Act
    migration = migrate_cards(OLD, NEW, store=board)
    # Assert
    assert set(migration.moved) == expected


def test_migrate_on_an_agent_with_no_cards_moves_nothing(board: Path):
    # Arrange
    nobody = "agent-with-nothing"
    # Act
    migration = migrate_cards(nobody, NEW, store=board)
    # Assert
    assert migration.total == 0


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------


def test_undo_hands_every_card_back_to_the_old_owner(board: Path, owned: list):
    # Arrange
    migration = migrate_cards(OLD, NEW, store=board)
    # Act
    undo_migrate_cards(migration)
    # Assert
    assert set(find_owned_cards(OLD, store=board)) == set(owned)


def test_undo_restores_the_original_scope(board: Path, owned: list):
    # Arrange
    migration = migrate_cards(OLD, NEW, store=board)
    # Act
    undo_migrate_cards(migration)
    # Assert
    assert _scope_of(board, owned[0]) == f"agent:{OLD}"


def test_undo_reports_no_failures_on_a_healthy_store(board: Path, owned: list):
    # Arrange
    migration = migrate_cards(OLD, NEW, store=board)
    # Act
    failed = undo_migrate_cards(migration)
    # Assert
    assert failed == []
