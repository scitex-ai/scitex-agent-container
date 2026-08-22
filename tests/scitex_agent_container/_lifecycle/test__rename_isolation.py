"""PROVE the rename tests cannot reach the live fleet or the live board.

This file exists because the failure mode it guards against is a
catastrophe, not a flake: a rename test that escaped its tmp root would
move a LIVE agent's spec dir, overlay and state dir, and REASSIGN ITS
CARDS on the real board. "The fixture sets $HOME, so we're isolated" is
exactly the assumption that has already burned this codebase once — sac's
own module-level path constants are computed at IMPORT time, so an env var
set afterwards cannot redirect them.

So: assert the isolation, do not assume it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._rename_plan import Layout

from .._helpers.fleet_root import (
    isolated_board,
    isolated_root,
    make_fleet,
    no_root_override,
)


@pytest.fixture
def board(tmp_path: Path):
    yield from isolated_board(tmp_path)


@pytest.fixture
def sac_root(tmp_path: Path):
    yield from isolated_root(tmp_path)


@pytest.fixture
def bare_env(tmp_path: Path):
    yield from no_root_override(tmp_path)


def test_every_layout_path_stays_inside_the_injected_root(tmp_path: Path):
    """No Layout path may escape the root a test injected."""
    # Arrange
    layout = Layout(root=tmp_path / "fleet")
    # Act
    paths = [
        layout.state_db,
        layout.spec_dir("a"),
        layout.spec_file("a"),
        layout.overlay_dir("a"),
        layout.runtime_dir("a"),
        layout.registry_json("a"),
    ]
    # Assert
    assert all(p.is_relative_to(tmp_path) for p in paths)


@pytest.mark.usefixtures("bare_env")
def test_layout_default_resolves_to_the_real_fleet_root():
    """Document WHY every test injects a Layout instead of using the default.

    With no override, ``Layout.default()`` IS the production root. Asserting
    that here makes the danger explicit rather than tribal knowledge: any
    test that calls it unguarded is touching the live fleet.
    """
    # Arrange
    expected = Path.home() / ".scitex" / "agent-container"
    # Act
    default_root = Layout.default().root
    # Assert
    assert default_root == expected


def test_the_root_env_port_redirects_layout_default(sac_root: Path):
    """The seam that lets the CLI test run without touching the live fleet.

    The CLI resolves its own ``Layout.default()`` — there is no ``--root``
    flag to pass. So the port MUST be honoured at CALL time; an import-time
    constant would leave the CliRunner test renaming a real agent.
    """
    # Arrange
    expected = sac_root
    # Act
    default_root = Layout.default().root
    # Assert
    assert default_root == expected


def test_isolated_board_redirects_scitex_todos_default_store(board: Path):
    """Even a call that FORGOT ``store=`` must land in tmp, not on the board.

    ``store=None`` makes scitex-todo resolve its default store. If that
    still resolved to the live 1,400-card board, one missing keyword in the
    rename code would reassign real cards. The fixture points
    ``$SCITEX_TODO_TASKS_YAML_SHARED`` at the tmp store, and scitex-cards
    reads that env var at CALL time — so this holds.

    Skips when the optional peer is absent (sac's own CI); there is no
    default store to redirect then, and faking one would prove nothing.
    """
    # Arrange
    # Skip only when the optional peer is genuinely ABSENT; if it is present
    # but the submodule path has moved, FAIL. `importorskip` on the full
    # dotted path cannot tell those apart — ModuleNotFoundError is an
    # ImportError subclass, so a rename or deletion becomes a silent skip,
    # which is what scitex_todo._store had already become here.
    pytest.importorskip("scitex_cards")
    _store = importlib.import_module("scitex_cards._store")
    # Act
    resolved = _store.resolve_tasks_path()
    # Assert
    assert resolved == board


def test_make_fleet_creates_the_spec_file(tmp_path: Path):
    # Arrange
    root = tmp_path / "fleet"
    # Act
    layout = make_fleet(root, "demo")
    # Assert
    assert layout.spec_file("demo").is_file()


def test_make_fleet_creates_the_overlay_dir(tmp_path: Path):
    # Arrange
    root = tmp_path / "fleet"
    # Act
    layout = make_fleet(root, "demo")
    # Assert
    assert layout.overlay_dir("demo").is_dir()


def test_make_fleet_creates_the_runtime_dir(tmp_path: Path):
    # Arrange
    root = tmp_path / "fleet"
    # Act
    layout = make_fleet(root, "demo")
    # Assert
    assert layout.runtime_dir("demo").is_dir()


def test_make_fleet_creates_the_registry_entry(tmp_path: Path):
    # Arrange
    root = tmp_path / "fleet"
    # Act
    layout = make_fleet(root, "demo")
    # Assert
    assert layout.registry_json("demo").is_file()
