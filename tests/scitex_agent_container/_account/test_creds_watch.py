"""Tests for the credential auto-sync watcher (``_account.creds_watch``).

No mocks (PA-306): the watcher's engine-call path and poll fallback are
driven against a real tmp home + a real ``io.StringIO`` log sink and a
hand-rolled fake ``sleep_fn``. The inotify backend itself is not
exercised here (it needs the ``inotifywait`` binary); the poll loop
shares the same engine call so it covers the sync behaviour.

AAA markers (TQ002), descriptive names (TQ003), one assertion each
(TQ007).
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._account.creds_watch import (
    default_log_path,
    run_sync_once,
    watch_poll,
)


@pytest.fixture
def _isolate_home(tmp_path: Path):
    """Force Path.home() inside tmp_path. PA-306: env save/restore."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _write_live(home: Path, email: str, expires_at_ms: int) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}})
    )
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )


def _store_snapshot_path(home: Path, slug: str) -> Path:
    return (
        home / ".scitex" / "agent-container" / "accounts" / slug / ".credentials.json"
    )


# ---------------------------------------------------------------------------
# default_log_path
# ---------------------------------------------------------------------------


def test_default_log_path_under_runtime_logs(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    # Act
    path = default_log_path(home=home)
    # Assert
    assert path == (
        home / ".scitex" / "agent-container" / "runtime" / "logs" / "creds-watch.log"
    )


# ---------------------------------------------------------------------------
# run_sync_once — engine-call path
# ---------------------------------------------------------------------------


def test_run_sync_once_saves_store_for_valid_live(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert _store_snapshot_path(home, "wyusuuke-gmail-com").exists()


def test_run_sync_once_logs_saved_action(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert "saved wyusuuke-gmail-com" in log.getvalue()


def test_run_sync_once_logs_invalid_without_raising(_isolate_home: Path) -> None:
    # Arrange — expired live cred: the watcher must log, not crash.
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() - 10_000) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert "live-cred-invalid" in log.getvalue()


def test_run_sync_once_does_not_save_for_expired_live(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() - 10_000) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert not _store_snapshot_path(home, "wyusuuke-gmail-com").exists()


# ---------------------------------------------------------------------------
# watch_poll — poll fallback against a tmp file
# ---------------------------------------------------------------------------


def test_watch_poll_initial_sync_writes_store(_isolate_home: Path) -> None:
    # Arrange — valid live cred; zero poll iterations means only the
    # initial sync runs.
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    watch_poll(log, home=home, iterations=0, sleep_fn=lambda _s: None)
    # Assert
    assert _store_snapshot_path(home, "wyusuuke-gmail-com").exists()


def test_watch_poll_resyncs_after_live_cred_change(_isolate_home: Path) -> None:
    # Arrange — start with cred A, then a sleep_fn that rewrites the live
    # cred to a fresher expiry on the first poll cycle so the loop must
    # detect the change and re-sync.
    home = _isolate_home
    first_ms = int((time.time() + 3_600) * 1_000)
    second_ms = int((time.time() + 9_999) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", first_ms)
    log = io.StringIO()

    def _mutate_on_first_poll(_seconds: float) -> None:
        _write_live(home, "wyusuuke@gmail.com", second_ms)

    # Act
    watch_poll(home=home, log=log, iterations=1, sleep_fn=_mutate_on_first_poll)
    # Assert — the store now holds the SECOND (fresher) expiry.
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    assert json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"] == second_ms


def test_watch_poll_logs_change_detected_on_mutation(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()

    def _mutate(_seconds: float) -> None:
        _write_live(home, "wyusuuke@gmail.com", int((time.time() + 9_999) * 1_000))

    # Act
    watch_poll(home=home, log=log, iterations=1, sleep_fn=_mutate)
    # Assert
    assert "change detected" in log.getvalue()


def test_watch_poll_no_change_does_not_log_change_detected(
    _isolate_home: Path,
) -> None:
    # Arrange — sleep_fn leaves the file untouched, so no change fires.
    home = _isolate_home
    _write_live(home, "wyusuuke@gmail.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    watch_poll(home=home, log=log, iterations=2, sleep_fn=lambda _s: None)
    # Assert
    assert "change detected" not in log.getvalue()
