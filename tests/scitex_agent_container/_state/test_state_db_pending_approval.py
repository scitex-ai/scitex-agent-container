"""Tests for the pending-prompt flag store (task #27).

Lead's design amendment (2026-06-01) replaces the prior held-message
queue with a minimal "is there a pending decision yes/no" flag. The
sender's original content is NEVER stored here — receivers decide on
identity, not on content. This module covers the flag CRUD in
isolation; the integration (denied send records the flag, the CLI
decision clears it) lives in its own test file.

No-mocks (PA-306): real on-disk state.db, env + module constant
save/restore. Each test: AAA markers (TQ002), one assertion (TQ007),
3+-word name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_pending_approval import (
    clear_pending_prompt,
    has_pending_prompt,
    record_pending_prompt,
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
# record_pending_prompt — first-wins flag semantics
# ---------------------------------------------------------------------------


def test_first_record_returns_true(isolated_state: Path) -> None:
    # Arrange — fresh DB, no prior row.
    # Act
    first = record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Assert — caller uses this signal to "emit the receiver-facing
    # push exactly once per pair until a decision".
    assert first is True


def test_second_record_returns_false_no_duplicate_push(
    isolated_state: Path,
) -> None:
    # Arrange — a pending row already exists.
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    second = record_pending_prompt(
        sender="alice", target="lead", db_path=isolated_state
    )
    # Assert — duplicate denied attempts MUST NOT re-prompt.
    assert second is False


def test_different_sender_target_pairs_both_emit(isolated_state: Path) -> None:
    # Arrange — dedupe is per-pair, not global.
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    second_pair = record_pending_prompt(
        sender="bob", target="lead", db_path=isolated_state
    )
    # Assert
    assert second_pair is True


# ---------------------------------------------------------------------------
# has_pending_prompt — read-only flag check
# ---------------------------------------------------------------------------


def test_has_pending_prompt_returns_true_after_record(
    isolated_state: Path,
) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    flag = has_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Assert
    assert flag is True


def test_has_pending_prompt_returns_false_for_absent_pair(
    isolated_state: Path,
) -> None:
    # Arrange — no record_pending_prompt call.
    # Act
    flag = has_pending_prompt(sender="ghost", target="lead", db_path=isolated_state)
    # Assert
    assert flag is False


# ---------------------------------------------------------------------------
# clear_pending_prompt — drop on decision
# ---------------------------------------------------------------------------


def test_clear_removes_pending_row(isolated_state: Path) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    clear_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    flag = has_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Assert
    assert flag is False


def test_clear_returns_true_when_row_existed(isolated_state: Path) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    removed = clear_pending_prompt(
        sender="alice", target="lead", db_path=isolated_state
    )
    # Assert
    assert removed is True


def test_clear_returns_false_when_row_absent(isolated_state: Path) -> None:
    # Arrange — no prior record.
    # Act
    removed = clear_pending_prompt(
        sender="ghost", target="lead", db_path=isolated_state
    )
    # Assert
    assert removed is False


def test_after_clear_next_record_returns_true_again(
    isolated_state: Path,
) -> None:
    # Arrange — record + clear + record again. The second record
    # MUST return True (i.e. emit the prompt) because the prior
    # decision was already made.
    record_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    clear_pending_prompt(sender="alice", target="lead", db_path=isolated_state)
    # Act
    second = record_pending_prompt(
        sender="alice", target="lead", db_path=isolated_state
    )
    # Assert
    assert second is True


# ---------------------------------------------------------------------------
# Fail-loud — empty sender / target rejected
# ---------------------------------------------------------------------------


def test_empty_sender_raises(isolated_state: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        record_pending_prompt(sender="", target="lead", db_path=isolated_state)


def test_empty_target_raises(isolated_state: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        record_pending_prompt(sender="alice", target="", db_path=isolated_state)
