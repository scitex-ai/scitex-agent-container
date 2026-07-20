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
    _signature,
    default_log_path,
    run_sync_once,
    watch_poll,
)

# A whole-second ns timestamp. Whole-second so the float(st_mtime)
# conversion is EXACT, which makes the sub-ulp test below deterministic
# rather than dependent on where the rounding boundary happens to fall.
_PINNED_NS = 1_784_352_999_000_000_000

# The live credential bundle's real size on this fleet. Successive
# rotations were measured at exactly this length, which is why st_size
# alone cannot detect a token rotation.
_CRED_BYTES = 1_102


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
    _write_live(home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert _store_snapshot_path(home, "alpha-example-com").exists()


def test_run_sync_once_logs_saved_action(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert "saved alpha-example-com" in log.getvalue()


def test_run_sync_once_logs_invalid_without_raising(_isolate_home: Path) -> None:
    # Arrange — expired live cred: the watcher must log, not crash.
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() - 10_000) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert "live-cred-invalid" in log.getvalue()


def test_run_sync_once_does_not_save_for_expired_live(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() - 10_000) * 1_000))
    log = io.StringIO()
    # Act
    run_sync_once(log, home=home)
    # Assert
    assert not _store_snapshot_path(home, "alpha-example-com").exists()


# ---------------------------------------------------------------------------
# watch_poll — poll fallback against a tmp file
# ---------------------------------------------------------------------------


def test_watch_poll_initial_sync_writes_store(_isolate_home: Path) -> None:
    # Arrange — valid live cred; zero poll iterations means only the
    # initial sync runs.
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    watch_poll(log, home=home, iterations=0, sleep_fn=lambda _s: None)
    # Assert
    assert _store_snapshot_path(home, "alpha-example-com").exists()


def test_watch_poll_resyncs_after_live_cred_change(_isolate_home: Path) -> None:
    # Arrange — start with cred A, then a sleep_fn that rewrites the live
    # cred to a fresher expiry on the first poll cycle so the loop must
    # detect the change and re-sync.
    home = _isolate_home
    first_ms = int((time.time() + 3_600) * 1_000)
    second_ms = int((time.time() + 9_999) * 1_000)
    _write_live(home, "alpha@example.com", first_ms)
    log = io.StringIO()

    def _mutate_on_first_poll(_seconds: float) -> None:
        _write_live(home, "alpha@example.com", second_ms)

    # Act
    watch_poll(home=home, log=log, iterations=1, sleep_fn=_mutate_on_first_poll)
    # Assert — the store now holds the SECOND (fresher) expiry.
    snap = _store_snapshot_path(home, "alpha-example-com")
    assert json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"] == second_ms


def test_watch_poll_logs_change_detected_on_mutation(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()

    def _mutate(_seconds: float) -> None:
        _write_live(home, "alpha@example.com", int((time.time() + 9_999) * 1_000))

    # Act
    watch_poll(home=home, log=log, iterations=1, sleep_fn=_mutate)
    # Assert
    assert "change detected" in log.getvalue()


def test_watch_poll_no_change_does_not_log_change_detected(
    _isolate_home: Path,
) -> None:
    # Arrange — sleep_fn leaves the file untouched, so no change fires.
    home = _isolate_home
    _write_live(home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    log = io.StringIO()
    # Act
    watch_poll(home=home, log=log, iterations=2, sleep_fn=lambda _s: None)
    # Assert
    assert "change detected" not in log.getvalue()


# ---------------------------------------------------------------------------
# _signature — the change-detection EQUALITY key
#
# A term missing from this key is a token rotation the watcher never
# reports. Each test below pins the OTHER two terms so it fails if and
# only if its own term is dropped — i.e. each is a mutation test for one
# element of the tuple.
#
# Note on method: timestamps are pinned with os.utime(ns=...) rather than
# produced by sleeping. A test that merely writes twice in quick
# succession does NOT discriminate the old key from the new one on a
# nanosecond-granularity filesystem — both pass — which is exactly how
# the float-mtime defect survived review. Pinning states the intended
# collision directly instead of hoping the scheduler produces it.
# ---------------------------------------------------------------------------


def test_signature_is_none_for_absent_file(tmp_path: Path) -> None:
    # Arrange — no live credential on disk yet.
    missing = tmp_path / ".credentials.json"
    # Act
    sig = _signature(missing)
    # Assert
    assert sig is None


def test_signature_changes_when_mtime_moves_below_float_precision(
    tmp_path: Path,
) -> None:
    # Arrange — two same-length writes 1 ns apart. At a 2026 epoch a
    # double's ulp is ~477 ns, so both timestamps convert to the SAME
    # st_mtime float: the old (st_mtime, st_size) key cannot see this.
    # Size and inode are held constant (in-place rewrite), so mtime_ns is
    # the only term that can distinguish them.
    path = tmp_path / ".credentials.json"
    path.write_text("a" * _CRED_BYTES)
    os.utime(path, ns=(_PINNED_NS, _PINNED_NS))
    before = _signature(path)
    path.write_text("b" * _CRED_BYTES)
    os.utime(path, ns=(_PINNED_NS + 1, _PINNED_NS + 1))
    # Act
    after = _signature(path)
    # Assert
    assert after != before


def test_signature_changes_when_only_inode_moves(tmp_path: Path) -> None:
    # Arrange — an atomic tmp+os.replace rotation, which is how BOTH
    # credential writers we control update the file. mtime and size are
    # forced identical across the swap, so the inode is the only term
    # left that can reveal the rotation.
    path = tmp_path / ".credentials.json"
    path.write_text("a" * _CRED_BYTES)
    os.utime(path, ns=(_PINNED_NS, _PINNED_NS))
    before = _signature(path)
    rotated = tmp_path / ".credentials.json.tmp"
    rotated.write_text("b" * _CRED_BYTES)
    os.replace(rotated, path)
    os.utime(path, ns=(_PINNED_NS, _PINNED_NS))
    # Act
    after = _signature(path)
    # Assert
    assert after != before


def test_signature_changes_when_only_size_moves(tmp_path: Path) -> None:
    # Arrange — models a coarse (whole-second) filesystem by pinning the
    # timestamp identical across an in-place rewrite that keeps the same
    # inode. Size is then the only term that can see the change, which is
    # why it stays in the key even though mtime_ns is finer-grained.
    path = tmp_path / ".credentials.json"
    path.write_text("a" * _CRED_BYTES)
    os.utime(path, ns=(_PINNED_NS, _PINNED_NS))
    before = _signature(path)
    path.write_text("a" * (_CRED_BYTES * 2))
    os.utime(path, ns=(_PINNED_NS, _PINNED_NS))
    # Act
    after = _signature(path)
    # Assert
    assert after != before
