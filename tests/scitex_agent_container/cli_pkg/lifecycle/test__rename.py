"""``sac agents rename`` through the real CLI.

NO MOCKS: a real ``CliRunner`` drives the real click command against a real
on-disk fleet. The command resolves its OWN ``Layout.default()`` — there is
no ``--root`` flag — so isolation comes from the
``$SCITEX_AGENT_CONTAINER_ROOT`` port, which is read at call time. Without
that, this file would rename a live agent.

Most tests here drive ``--no-cards``, so they exercise the CLI everywhere
including sac's own CI, where the optional ``scitex-todo`` peer is absent.
The card-bearing tests ``importorskip`` it individually rather than taking
the whole file down with them — the CLI's refusals, its plan rendering and
its confirmation gate are worth CI coverage on their own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._lifecycle._rename_plan import Layout
from scitex_agent_container.cli_pkg.lifecycle._rename import rename

from ..._helpers.fleet_root import (
    isolated_board,
    isolated_root,
    make_fleet,
    seed_identity_and_history,
)

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture
def sac_root(tmp_path: Path):
    yield from isolated_root(tmp_path)


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def fleet(sac_root: Path) -> Layout:
    """A real agent on disk, with rows in state.db. No board."""
    layout = make_fleet(sac_root, OLD)
    seed_identity_and_history(layout, OLD)
    return layout


def _run(*argv: str):
    return CliRunner().invoke(rename, list(argv))


@pytest.fixture
def dry_run(fleet: Layout):
    return _run(OLD, NEW, "--dry-run", "--no-cards")


@pytest.fixture
def applied(fleet: Layout):
    return _run(OLD, NEW, "-y", "--no-cards")


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
    """The spec change that orphans the cards must be impossible to miss."""
    # Arrange
    needle = "BOARD IDENTITY"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_reports_the_state_db_rows(dry_run):
    # Arrange — ``comms_nodes.name`` until 2026-08-28; that table moved to
    # PostgreSQL and left ``NAME_COLUMNS``, so it is no longer among the
    # state.db counts the dry run prints.
    needle = "definitions.name"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_reports_the_overlay_move(dry_run):
    # Arrange
    needle = "overlay-dir"
    # Act
    output = dry_run.output
    # Assert
    assert needle in output


def test_dry_run_warns_that_no_cards_orphans_the_board(dry_run):
    """The escape hatch has to say what it costs."""
    # Arrange
    needle = "ORPHANED"
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


# ---------------------------------------------------------------------------
# The real run
# ---------------------------------------------------------------------------


def test_rename_exits_clean(pg_schema: str, applied):
    # Arrange
    expected = 0
    # Act
    code = applied.exit_code
    # Assert
    assert code == expected, applied.output


def test_rename_moves_the_spec_dir(pg_schema: str, applied, fleet: Layout):
    # Arrange
    new_spec = fleet.spec_file(NEW)
    # Act
    exists = new_spec.is_file()
    # Assert
    assert exists


def test_rename_tells_the_operator_how_to_start_the_agent(pg_schema: str, applied):
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
    result = _run(unknown, NEW, "-y", "--no-cards")
    # Assert
    assert result.exit_code != 0


def test_the_unknown_agent_refusal_says_not_found(fleet: Layout):
    # Arrange
    unknown = "no-such-agent"
    # Act
    result = _run(unknown, NEW, "-y", "--no-cards")
    # Assert
    assert "not found" in result.output


def test_rename_refuses_when_the_target_name_is_taken(fleet: Layout):
    # Arrange
    make_fleet(fleet.root, NEW)
    # Act
    result = _run(OLD, NEW, "-y", "--no-cards")
    # Assert
    assert "already exists" in result.output


def test_rename_refuses_without_yes(fleet: Layout):
    """REFUSE, never prompt (ecosystem CLI §2).

    An interactive confirm would hang forever under cron, CI, or an agent's
    non-tty shell — on a verb that has already been asked to move a live
    agent's directories.
    """
    # Arrange
    expected = 2
    # Act
    result = _run(OLD, NEW, "--no-cards")
    # Assert
    assert result.exit_code == expected


def test_the_refusal_without_yes_names_the_flag(fleet: Layout):
    # Arrange
    expected = "--yes/-y"
    # Act
    result = _run(OLD, NEW, "--no-cards")
    # Assert
    assert expected in result.output


def test_refusing_without_yes_moves_nothing(fleet: Layout):
    """A refusal must be a no-op, not a half-rename."""
    # Arrange
    old_dir = fleet.spec_dir(OLD)
    # Act
    _run(OLD, NEW, "--no-cards")
    # Assert
    assert old_dir.is_dir()


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_dry_run_reports_the_new_name(fleet: Layout):
    # Arrange
    expected = NEW
    # Act
    result = _run(OLD, NEW, "--dry-run", "--json", "--no-cards")
    # Assert
    assert json.loads(result.stdout)["new"] == expected


def test_json_dry_run_lists_the_current_board_identity_change(fleet: Layout):
    # Arrange: the spelling most live specs use. The plan missed it until
    # 2026-08-25 because every fixture here carried the retired one.
    needle = "SCITEX_CARDS_AGENT_ID"
    # Act
    result = _run(OLD, NEW, "--dry-run", "--json", "--no-cards")
    # Assert
    assert any(
        needle in c["path"] for c in json.loads(result.stdout)["spec_changes"]
    )


def test_json_dry_run_lists_the_spec_changes(fleet: Layout):
    # Arrange
    needle = "SCITEX_TODO_AGENT_ID"
    # Act
    result = _run(OLD, NEW, "--dry-run", "--json", "--no-cards")
    # Assert
    assert any(
        needle in c["path"] for c in json.loads(result.stdout)["spec_changes"]
    )


# ---------------------------------------------------------------------------
# With a real board (skipped when the optional peer is absent)
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_card_count(fleet: Layout, board: Path):
    # Arrange
    _seed(board, 3)
    # Act
    result = _run(OLD, NEW, "--dry-run")
    # Assert
    assert "3 card(s)" in result.output


def test_dry_run_names_the_port_it_migrates_cards_through(
    fleet: Layout, board: Path
):
    """Ports and adapters, stated in the UI: sac calls the board's primitive."""
    # Arrange
    _seed(board, 1)
    # Act
    result = _run(OLD, NEW, "--dry-run")
    # Assert
    assert "reassign_task" in result.output


def test_dry_run_moves_no_card(fleet: Layout, board: Path):
    # Arrange
    _seed(board, 3)
    # Act
    _run(OLD, NEW, "--dry-run")
    # Assert
    assert len(_owned(OLD, board)) == 3


def test_rename_migrates_every_card(pg_schema: str, fleet: Layout, board: Path):
    # Arrange
    _seed(board, 3)
    # Act
    _run(OLD, NEW, "-y")
    # Assert
    assert _owned(OLD, board) == []


def _seed(board: Path, count: int) -> list[str]:
    """Seed real cards, skipping the test when the optional peer is absent.

    Names scitex_cards, NOT scitex_todo. scitex-cards v0.41.0 DELETED the
    scitex_todo module outright, and `importorskip` skips on
    ModuleNotFoundError — an ImportError subclass — so guarding on a deleted
    name turns this test into a permanent, silent SKIP. It would never fail
    and never run: green by absence.
    """
    pytest.importorskip("scitex_cards")
    from ..._helpers.fleet_root import seed_cards

    return seed_cards(board, OLD, count)


def _owned(name: str, board: Path) -> list[str]:
    from scitex_agent_container._lifecycle._rename_cards import find_owned_cards

    return find_owned_cards(name, store=board)
