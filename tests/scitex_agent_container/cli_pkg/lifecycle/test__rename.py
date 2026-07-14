"""``sac agents rename`` through the real CLI.

NO MOCKS: a real ``CliRunner`` drives the real click command against a real
on-disk fleet and a real scitex-todo store. The command resolves its OWN
``Layout.default()`` — there is no ``--root`` flag — so isolation comes
from the ``$SCITEX_AGENT_CONTAINER_ROOT`` port, which is read at call time.
Without that, this file would rename a live agent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._lifecycle._rename_cards import find_owned_cards
from scitex_agent_container._lifecycle._rename_plan import Layout
from scitex_agent_container.cli_pkg.lifecycle._rename import rename

from ..._helpers.fleet_root import (
    isolated_board,
    isolated_root,
    make_fleet,
    make_state_db,
    seed_cards,
)

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def sac_root(tmp_path: Path):
    yield from isolated_root(tmp_path)


@pytest.fixture
def fleet(sac_root: Path, board: Path) -> Layout:
    """A real agent on disk, with rows in state.db and cards on the board."""
    layout = make_fleet(sac_root, OLD)
    db = make_state_db(layout)
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO comms_nodes (name, host, a2a_port, registered_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            (OLD, "h", 9001, 1.0, 1.0),
        )
    conn.close()
    seed_cards(board, OLD, 3)
    return layout


def _run(*argv: str):
    return CliRunner().invoke(rename, list(argv))


@pytest.fixture
def dry_run(fleet: Layout):
    return _run(OLD, NEW, "--dry-run")


@pytest.fixture
def applied(fleet: Layout):
    return _run(OLD, NEW, "-y")


# ---------------------------------------------------------------------------
# --dry-run: exact, and touches nothing
# ---------------------------------------------------------------------------


def test_dry_run_exits_clean(dry_run):
    # Arrange
    expected = 0
    # Act
    code = dry_run.exit_code
    # Assert
    assert code == expected, dry_run.output


def test_dry_run_says_it_is_a_dry_run(dry_run):
    # Arrange
    needle = "DRY RUN"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_reports_the_board_identity_change(dry_run):
    """The change that orphans the cards must be impossible to miss."""
    # Arrange
    needle = "BOARD IDENTITY"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_reports_the_card_count(dry_run):
    # Arrange
    needle = "3 card(s)"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_reports_the_state_db_rows(dry_run):
    # Arrange
    needle = "comms_nodes.name"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_names_the_port_it_migrates_cards_through(dry_run):
    """Ports and adapters, stated in the UI: sac calls the board's primitive."""
    # Arrange
    needle = "reassign_task"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_leaves_the_spec_dir_where_it_was(dry_run, fleet: Layout):
    # Arrange
    old_dir = fleet.spec_dir(OLD)
    # Act
    exists = old_dir.is_dir()
    # Assert
    assert exists


def test_dry_run_moves_no_cards(dry_run, fleet: Layout, board: Path):
    # Arrange
    expected = 3
    # Act
    still_owned = find_owned_cards(OLD, store=board)
    # Assert
    assert len(still_owned) == expected


# ---------------------------------------------------------------------------
# The real run
# ---------------------------------------------------------------------------


def test_rename_exits_clean(applied):
    # Arrange
    expected = 0
    # Act
    code = applied.exit_code
    # Assert
    assert code == expected, applied.output


def test_rename_moves_the_spec_dir(applied, fleet: Layout):
    # Arrange
    new_spec = fleet.spec_file(NEW)
    # Act
    exists = new_spec.is_file()
    # Assert
    assert exists


def test_rename_migrates_every_card(applied, fleet: Layout, board: Path):
    # Arrange
    store = board
    # Act
    orphans = find_owned_cards(OLD, store=store)
    # Assert
    assert orphans == []


def test_rename_tells_the_operator_how_to_start_the_agent(applied):
    # Arrange
    needle = f"sac agents start {NEW}"
    # Act
    output = applied.output
    # Assert
    assert needle in output


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_rename_refuses_an_unknown_agent(fleet: Layout):
    # Arrange
    unknown = "no-such-agent"
    # Act
    result = _run(unknown, NEW, "-y")
    # Assert
    assert result.exit_code != 0


def test_the_unknown_agent_refusal_says_not_found(fleet: Layout):
    # Arrange
    unknown = "no-such-agent"
    # Act
    result = _run(unknown, NEW, "-y")
    # Assert
    assert "not found" in result.output


def test_rename_refuses_when_the_target_name_is_taken(fleet: Layout):
    # Arrange
    make_fleet(fleet.root, NEW)
    # Act
    result = _run(OLD, NEW, "-y")
    # Assert
    assert "already exists" in result.output


def test_json_mode_refuses_to_run_unconfirmed(fleet: Layout):
    """--json is non-interactive; a silent unconfirmed rename would be a trap."""
    # Arrange
    expected = "non-interactive"
    # Act
    result = _run(OLD, NEW, "--json")
    # Assert
    assert expected in result.output


def test_json_dry_run_emits_the_plan(fleet: Layout):
    # Arrange
    import json

    # Act
    result = _run(OLD, NEW, "--dry-run", "--json")
    # Assert
    assert json.loads(result.output)["cards"]["count"] == 3


def test_json_dry_run_reports_the_new_name(fleet: Layout):
    # Arrange
    import json

    # Act
    result = _run(OLD, NEW, "--dry-run", "--json")
    # Assert
    assert json.loads(result.output)["new"] == NEW
