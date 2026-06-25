"""Tests for ``_state.notification_blocker`` — card blocker on Notification.

No-mocks. Every test exercises real production code against a REAL temporary
``tasks.yaml`` mutated through the installed ``scitex-todo`` CLI:

* ``$SCITEX_TODO_TASKS`` points the CLI at a per-test temp store.
* ``$SCITEX_DIR`` + cwd-outside-a-repo route the event ring-buffer to
  ``tmp_path`` (user scope), so dedup uses a real on-disk JSONL.
* ``handle_notification`` runs the real ``scitex-todo list-tasks / update /
  comment`` subprocesses — no patches, no monkeypatch.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._state.notification_blocker import handle_notification

_AGENT = "agent:nbtest"


def _scitex_todo(store: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SCITEX_TODO_TASKS": str(store)}
    return subprocess.run(
        ["scitex-todo", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def _card(store: Path, card_id: str) -> dict:
    proc = _scitex_todo(store, ["list-tasks", "--json"])
    rows = json.loads(proc.stdout or "[]")
    return next(r for r in rows if r.get("id") == card_id)


@pytest.fixture
def todo_store(tmp_path: Path, env_save_restore):
    """A real temp tasks.yaml + event ring routed to ``tmp_path``.

    Yields the store path; cleans env / cwd on teardown.
    """
    store = tmp_path / "tasks.yaml"
    env_save_restore.set("SCITEX_TODO_TASKS", str(store))
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "scitex_home"))
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.delete("CLAUDE_AGENT_ID")
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield store
    finally:
        os.chdir(saved_cwd)


@pytest.fixture
def one_in_progress_card(todo_store: Path):
    """Seed exactly one in_progress card owned by ``_AGENT``."""
    _scitex_todo(
        todo_store,
        ["add", "card-1", "Active card", "--status", "in_progress",
         "--agent", _AGENT, "-y"],
    )
    return todo_store


# ---------------------------------------------------------------------------
# Happy path — one in_progress card
# ---------------------------------------------------------------------------


def test_handler_sets_operator_decision_blocker(one_in_progress_card: Path):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    # Act
    handle_notification(_AGENT, payload)
    # Assert
    assert _card(one_in_progress_card, "card-1")["blocker"] == "operator-decision"


def test_handler_sets_status_blocked(one_in_progress_card: Path):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    # Act
    handle_notification(_AGENT, payload)
    # Assert
    assert _card(one_in_progress_card, "card-1")["status"] == "blocked"


def test_handler_adds_comment_carrying_message(one_in_progress_card: Path):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    # Act
    handle_notification(_AGENT, payload)
    comments = _card(one_in_progress_card, "card-1").get("comments", [])
    # Assert
    assert any("Submit answers to continue" in str(c) for c in comments)


def test_handler_dedups_repeat_notification(one_in_progress_card: Path):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    handle_notification(_AGENT, payload)
    # Act
    handle_notification(_AGENT, payload)
    comments = _card(one_in_progress_card, "card-1").get("comments", [])
    # Assert
    assert len(comments) == 1


# ---------------------------------------------------------------------------
# Zero-card fail-loud path
# ---------------------------------------------------------------------------


def test_zero_card_writes_no_card(todo_store: Path):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    # Act
    handle_notification(_AGENT, payload)
    rows = json.loads(_scitex_todo(todo_store, ["list-tasks", "--json"]).stdout or "[]")
    # Assert
    assert rows == []


def test_zero_card_logs_loud_warning(todo_store: Path, caplog):
    # Arrange
    payload = {"message": "Submit answers to continue"}
    # Act
    with caplog.at_level("WARNING"):
        handle_notification(_AGENT, payload)
    # Assert
    assert "owns no in_progress card" in caplog.text
