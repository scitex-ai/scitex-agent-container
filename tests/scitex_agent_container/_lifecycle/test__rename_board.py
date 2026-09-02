"""The rename engine WITH the board attached — the cards must follow, or come back.

The half of the engine that needs a live ``scitex-todo``. Split out of
``test__rename.py`` because scitex-todo is an OPTIONAL peer: it is not a
declared dependency of sac and is absent from sac's own CI, so a
board-coupled test would ERROR there rather than run. Skipping is the
repo's established contract for a missing peer (see
``tests/integration/test_cross_package_imports.py``) — but only the
board-coupled tests may skip. The atomicity/rollback matrix in the sibling
module stays board-free precisely so it runs EVERYWHERE.

These tests still carry the point of the whole verb: rename with cards
present, and every card follows — or, when the rename fails, every card
comes back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("scitex_todo")

from scitex_agent_container._lifecycle._rename import (  # noqa: E402
    STEPS,
    agent_rename,
    apply_plan,
)
from scitex_agent_container._lifecycle._rename_cards import (  # noqa: E402
    find_owned_cards,
)
from scitex_agent_container._lifecycle._rename_plan import (  # noqa: E402
    Layout,
    RenameError,
    build_plan,
)

from .._helpers.fleet_root import (  # noqa: E402
    isolated_board,
    make_fleet,
    seed_cards,
    seed_identity_and_history,
)

OLD = "scitex-todo"
NEW = "scitex-cards"


class Boom(RuntimeError):
    """An injected mid-rename failure."""


def _raise_at(step_to_fail: str):
    """An ``on_step`` callback that aborts the rename at one step.

    Not a mock: ``on_step`` is the REAL progress hook the CLI passes on
    every run.
    """

    def _on_step(step: str) -> None:
        if step == step_to_fail:
            raise Boom(f"injected failure at {step}")

    return _on_step


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def layout(tmp_path: Path, pg_schema: str) -> Layout:
    # ``pg_schema``: ``seed_identity_and_history`` writes the history half to
    # the shared PostgreSQL since 2026-08-28 (ADR-0023).
    built = make_fleet(tmp_path / "fleet", OLD)
    seed_identity_and_history(OLD)
    return built


@pytest.fixture
def cards(board: Path) -> list[str]:
    """Two cards, not twenty.

    Every card write goes through the REAL scitex-todo store, whose
    per-write cost is ~3.2 s of fixed overhead (see
    ``_helpers.fleet_root.add_card``). Two proves "every card follows"; the
    card behaviour itself is covered exhaustively in ``test__rename_cards``.
    """
    return seed_cards(board, OLD, 2)


@pytest.fixture
def renamed(layout: Layout, board: Path, cards: list[str]) -> Layout:
    agent_rename(OLD, NEW, layout=layout, store=board)
    return layout


@pytest.fixture(params=STEPS, ids=list(STEPS))
def rolled_back(layout: Layout, board: Path, cards: list[str], request) -> Layout:
    """A rename that FAILED at ``request.param`` and rolled itself back."""
    plan = build_plan(OLD, NEW, layout=layout, store=board)
    try:
        apply_plan(plan, store=board, on_step=_raise_at(request.param))
    except RenameError:
        return layout
    raise AssertionError(f"apply_plan did not fail at step {request.param!r}")


# ---------------------------------------------------------------------------
# The plan sees the cards
# ---------------------------------------------------------------------------


def test_the_plan_counts_every_card_that_would_be_reassigned(
    layout: Layout, board: Path, cards: list[str]
):
    # Arrange
    expected = set(cards)
    # Act
    plan = build_plan(OLD, NEW, layout=layout, store=board)
    # Assert
    assert set(plan.card_ids) == expected


def test_building_a_plan_moves_no_card(
    layout: Layout, board: Path, cards: list[str]
):
    """--dry-run must be exactly that, on the board too."""
    # Arrange
    expected = set(cards)
    # Act
    build_plan(OLD, NEW, layout=layout, store=board)
    # Assert
    assert set(find_owned_cards(OLD, store=board)) == expected


# ---------------------------------------------------------------------------
# The rename moves them — THE point of the verb
# ---------------------------------------------------------------------------


def test_rename_leaves_no_card_orphaned(renamed: Layout, board: Path):
    """Rename with cards present: NOT ONE is left behind."""
    # Arrange
    store = board
    # Act
    orphans = find_owned_cards(OLD, store=store)
    # Assert
    assert orphans == []


def test_rename_gives_every_card_to_the_new_owner(
    renamed: Layout, board: Path, cards: list[str]
):
    # Arrange
    expected = set(cards)
    # Act
    owned = set(find_owned_cards(NEW, store=board))
    # Assert
    assert owned == expected


def test_rename_still_rewrites_the_board_identity_in_the_spec(renamed: Layout):
    """The card owner and the spec's SCITEX_TODO_AGENT_ID must agree."""
    # Arrange
    expected = f"SCITEX_TODO_AGENT_ID={NEW}"
    # Act
    text = renamed.spec_file(NEW).read_text()
    # Assert
    assert expected in text


# ---------------------------------------------------------------------------
# ...or the rollback brings them back — at EVERY step
# ---------------------------------------------------------------------------


def test_rollback_hands_every_card_back(
    rolled_back: Layout, board: Path, cards: list[str]
):
    """A `verify`-step failure lands AFTER the cards moved. They must return."""
    # Arrange
    expected = set(cards)
    # Act
    owned = set(find_owned_cards(OLD, store=board))
    # Assert
    assert owned == expected


def test_rollback_leaves_the_new_owner_holding_no_cards(
    rolled_back: Layout, board: Path
):
    # Arrange
    store = board
    # Act
    owned = find_owned_cards(NEW, store=store)
    # Assert
    assert owned == []
