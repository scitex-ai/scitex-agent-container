"""Tests for the ``comms_blocks`` table (task #27).

Lead's design amendment (2026-06-01): UNBLOCK is the existing
``grant_send`` (write ``comms_grants``); BLOCK is this module's
``block_send`` (write ``comms_blocks``) — symmetric helpers, same
shape as ``state_db_nodes`` grant/revoke/has helpers.

No-mocks (PA-306): real on-disk state.db. AAA markers, one
assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_blocks import (
    block_send,
    has_block,
    unblock_send,
)


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


# ---------------------------------------------------------------------------
# block_send + has_block — basic persistence
# ---------------------------------------------------------------------------


def test_block_then_has_block_returns_true(isolated_state: Path) -> None:
    # Arrange
    block_send(sender="alice", target="lead", db_path=isolated_state)
    # Act
    flag = has_block(sender="alice", target="lead", db_path=isolated_state)
    # Assert
    assert flag is True


def test_has_block_returns_false_for_absent_pair(isolated_state: Path) -> None:
    # Arrange — no block_send.
    # Act
    flag = has_block(sender="ghost", target="lead", db_path=isolated_state)
    # Assert
    assert flag is False


def test_block_is_directional(isolated_state: Path) -> None:
    # Arrange — block alice → lead. The REVERSE direction
    # (lead → alice) must be unaffected.
    block_send(sender="alice", target="lead", db_path=isolated_state)
    # Act
    reverse_flag = has_block(sender="lead", target="alice", db_path=isolated_state)
    # Assert
    assert reverse_flag is False


def test_block_is_idempotent(isolated_state: Path) -> None:
    # Arrange — block twice. The row's timestamp must not bump on
    # the second call (mirrors grant_send semantics).
    block_send(sender="alice", target="lead", note="first", db_path=isolated_state)
    # Act
    block_send(sender="alice", target="lead", note="second", db_path=isolated_state)
    # Assert — still exactly one row's worth of state visible via
    # has_block; the test pins the public observable, not the row
    # count (idempotency is the contract).
    assert has_block(sender="alice", target="lead", db_path=isolated_state)


# ---------------------------------------------------------------------------
# unblock_send — remove the row
# ---------------------------------------------------------------------------


def test_unblock_removes_the_block_row(isolated_state: Path) -> None:
    # Arrange
    block_send(sender="alice", target="lead", db_path=isolated_state)
    unblock_send(sender="alice", target="lead", db_path=isolated_state)
    # Act
    flag = has_block(sender="alice", target="lead", db_path=isolated_state)
    # Assert
    assert flag is False


def test_unblock_returns_true_when_row_existed(isolated_state: Path) -> None:
    # Arrange
    block_send(sender="alice", target="lead", db_path=isolated_state)
    # Act
    removed = unblock_send(sender="alice", target="lead", db_path=isolated_state)
    # Assert
    assert removed is True


def test_unblock_returns_false_when_row_absent(isolated_state: Path) -> None:
    # Arrange — no prior block.
    # Act
    removed = unblock_send(sender="ghost", target="lead", db_path=isolated_state)
    # Assert
    assert removed is False


# ---------------------------------------------------------------------------
# Fail-loud — empty inputs rejected
# ---------------------------------------------------------------------------


def test_block_empty_sender_raises(isolated_state: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        block_send(sender="", target="lead", db_path=isolated_state)


def test_block_empty_target_raises(isolated_state: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        block_send(sender="alice", target="", db_path=isolated_state)
