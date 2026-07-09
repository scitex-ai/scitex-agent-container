"""Unit tests for ``_account.active_login_write``.

The module mirrors a freshly-rotated snapshot token block into the
operator's LIVE ``~/.claude/.credentials.json`` under a
backup -> atomic-replace -> verify-or-restore contract. These tests run
entirely on tmp files — they NEVER touch a real ``~/.claude`` or real
snapshot — and drive the corrupt-write path through the injectable
``serialize`` production seam (not a mock).

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._account.active_login_write import (
    ActiveLoginSyncError,
    read_refresh_token,
    sync_active_login,
)

_FUTURE_MS = 9_999_999_999_000  # far-future expiry


def _write_creds(
    path: Path,
    *,
    access: str,
    refresh: str,
    expires_ms: int = _FUTURE_MS,
    extra: dict | None = None,
    wrapped: bool = True,
) -> Path:
    block = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_ms,
    }
    data: dict = {"claudeAiOauth": block} if wrapped else dict(block)
    if extra:
        data.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _run_expecting_error(live: Path, snap: Path, serialize=None) -> Exception | None:
    """Run the sync and RETURN any ActiveLoginSyncError (else None)."""
    try:
        if serialize is None:
            sync_active_login(live, snap)
        else:
            sync_active_login(live, snap, serialize=serialize)
    except ActiveLoginSyncError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# read_refresh_token
# ---------------------------------------------------------------------------


def test_read_refresh_token_returns_wrapped_value(tmp_path) -> None:
    # Arrange
    p = _write_creds(tmp_path / "c.json", access="A", refresh="THE-REFRESH")
    # Act
    got = read_refresh_token(p)
    # Assert
    assert got == "THE-REFRESH"


def test_read_refresh_token_missing_file_is_none(tmp_path) -> None:
    # Arrange
    missing = tmp_path / "nope.json"
    # Act
    got = read_refresh_token(missing)
    # Assert
    assert got is None


# ---------------------------------------------------------------------------
# Happy path: mirror + structure preservation
# ---------------------------------------------------------------------------


def test_sync_writes_new_access_token_into_live(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    sync_active_login(live, snap)
    # Assert
    got = json.loads(live.read_text())["claudeAiOauth"]["accessToken"]
    assert got == "NEW-ACCESS"


def test_sync_writes_rotated_refresh_token_into_live(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    sync_active_login(live, snap)
    # Assert
    got = json.loads(live.read_text())["claudeAiOauth"]["refreshToken"]
    assert got == "NEW-REF"


def test_sync_preserves_other_live_keys(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(
        tmp_path / "live.json",
        access="OLD-ACCESS",
        refresh="OLD-REF",
        extra={"unrelatedRootKey": "keep-me"},
    )
    # Act
    sync_active_login(live, snap)
    # Assert
    assert json.loads(live.read_text())["unrelatedRootKey"] == "keep-me"


def test_sync_preserves_sibling_oauth_fields(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = tmp_path / "live.json"
    live.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "refreshToken": "OLD-REF",
                    "expiresAt": _FUTURE_MS,
                    "clientId": "keep-this-cid",
                }
            }
        )
    )
    # Act
    sync_active_login(live, snap)
    # Assert
    assert json.loads(live.read_text())["claudeAiOauth"]["clientId"] == "keep-this-cid"


# ---------------------------------------------------------------------------
# Verify-or-restore
# ---------------------------------------------------------------------------


def test_corrupt_write_raises(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    err = _run_expecting_error(live, snap, serialize=lambda _d: "{}")
    # Assert
    assert isinstance(err, ActiveLoginSyncError)


def test_corrupt_write_restores_original_live_content(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    _run_expecting_error(live, snap, serialize=lambda _d: "not json at all {")
    # Assert
    assert json.loads(live.read_text())["claudeAiOauth"]["accessToken"] == "OLD-ACCESS"


def test_corrupt_write_leaves_backup_alongside(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    _run_expecting_error(live, snap, serialize=lambda _d: "{}")
    # Assert
    assert Path(str(live) + ".bak").is_file()


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_missing_snapshot_tokens_raises(tmp_path) -> None:
    # Arrange
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"claudeAiOauth": {}}))
    live = _write_creds(tmp_path / "live.json", access="OLD-ACCESS", refresh="OLD-REF")
    # Act
    err = _run_expecting_error(live, snap)
    # Assert
    assert isinstance(err, ActiveLoginSyncError)


def test_missing_live_file_raises(tmp_path) -> None:
    # Arrange
    snap = _write_creds(tmp_path / "snap.json", access="NEW-ACCESS", refresh="NEW-REF")
    absent = tmp_path / "absent.json"
    # Act
    err = _run_expecting_error(absent, snap)
    # Assert
    assert isinstance(err, ActiveLoginSyncError)
