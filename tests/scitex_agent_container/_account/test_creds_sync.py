"""Tests for the credential auto-sync engine (``_account.creds_sync``).

No mocks (PA-306): every test drives the real engine against a tmp
home/store built from real JSON files. AAA markers (TQ002), descriptive
names (TQ003), one assertion each (TQ007).

The ``_isolate_home`` fixture forces ``$HOME`` inside ``tmp_path`` so a
``_store_path`` regression can never write to the operator's real
``~/.scitex/agent-container/accounts/``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._account.creds_sync import (
    AccountIdentityError,
    Freshness,
    LiveCredInvalidError,
    account_freshness,
    slugify_email,
    sync_live,
)


@pytest.fixture
def _isolate_home(tmp_path: Path):
    """Force Path.home() inside tmp_path for the test's duration.

    PA-306: no monkeypatch — Path.home() reads $HOME on Unix, so an
    explicit os.environ save/restore is the real equivalent.
    """
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
    """Write a real live credential + ~/.claude.json under ``home``."""
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}})
    )
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )


def _store_snapshot_path(home: Path, slug: str) -> Path:
    """Return the on-disk store credential path for a slug under ``home``."""
    return (
        home / ".scitex" / "agent-container" / "accounts" / slug / ".credentials.json"
    )


# ---------------------------------------------------------------------------
# slugify_email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "slug"),
    [
        ("wyusuuke@gmail.com", "wyusuuke-gmail-com"),
        ("ywatanabe@scitex.ai", "ywatanabe-scitex-ai"),
        ("Mixed.Case@Example.COM", "mixed-case-example-com"),
    ],
)
def test_slugify_email_replaces_at_and_dot_with_hyphen(email: str, slug: str) -> None:
    # Arrange
    source = email
    # Act
    result = slugify_email(source)
    # Assert
    assert result == slug


# ---------------------------------------------------------------------------
# sync_live — happy path (store absent -> saved)
# ---------------------------------------------------------------------------


def test_sync_live_saves_when_store_absent(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    result = sync_live(home=home)
    # Assert
    assert result.action == "saved"


def test_sync_live_writes_store_snapshot_bytes_matching_live(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    sync_live(home=home)
    # Assert
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    assert json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"] == future_ms


def test_sync_live_derives_store_name_from_active_email(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "ywatanabe@scitex.ai", future_ms)
    # Act
    result = sync_live(home=home)
    # Assert
    assert result.store_name == "ywatanabe-scitex-ai"


def test_sync_live_writes_account_metadata_email(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    sync_live(home=home)
    # Assert
    meta = (
        home
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "wyusuuke-gmail-com"
        / "account.json"
    )
    assert json.loads(meta.read_text())["email_address"] == "wyusuuke@gmail.com"


# ---------------------------------------------------------------------------
# sync_live — idempotence
# ---------------------------------------------------------------------------


def test_sync_live_is_up_to_date_when_store_matches_live(
    _isolate_home: Path,
) -> None:
    # Arrange — first sync writes the store, second should be a no-op.
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    sync_live(home=home)
    # Act
    result = sync_live(home=home)
    # Assert
    assert result.action == "up-to-date"


def test_sync_live_overwrites_store_older_than_live(_isolate_home: Path) -> None:
    # Arrange — a stale store snapshot with an earlier expiry than live.
    home = _isolate_home
    older_ms = int((time.time() + 100) * 1_000)
    newer_ms = int((time.time() + 10_000) * 1_000)
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"claudeAiOauth": {"expiresAt": older_ms}}))
    _write_live(home, "wyusuuke@gmail.com", newer_ms)
    # Act
    result = sync_live(home=home)
    # Assert
    assert result.action == "saved"


def test_sync_live_overwrites_store_that_is_expired(_isolate_home: Path) -> None:
    # Arrange — store expired in the past; live is valid.
    home = _isolate_home
    expired_ms = int((time.time() - 10_000) * 1_000)
    future_ms = int((time.time() + 3_600) * 1_000)
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expired_ms}}))
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    result = sync_live(home=home)
    # Assert
    assert result.action == "saved"


# ---------------------------------------------------------------------------
# sync_live — fail-loud on invalid live cred
# ---------------------------------------------------------------------------


def test_sync_live_raises_when_live_cred_absent(_isolate_home: Path) -> None:
    # Arrange — no ~/.claude/.credentials.json at all.
    home = _isolate_home
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "x@y.com"}})
    )
    # Act
    ctx = pytest.raises(LiveCredInvalidError)
    # Assert
    with ctx:
        sync_live(home=home)


def test_sync_live_raises_when_live_cred_expired(_isolate_home: Path) -> None:
    # Arrange — live cred expired in the past; must NOT save a stale token.
    home = _isolate_home
    expired_ms = int((time.time() - 10_000) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", expired_ms)
    # Act
    ctx = pytest.raises(LiveCredInvalidError)
    # Assert
    with ctx:
        sync_live(home=home)


def test_sync_live_does_not_write_store_when_live_expired(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    expired_ms = int((time.time() - 10_000) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", expired_ms)
    # Act
    try:
        sync_live(home=home)
    except LiveCredInvalidError:
        pass
    # Assert — no snapshot was written for a stale live cred.
    assert not _store_snapshot_path(home, "wyusuuke-gmail-com").exists()


def test_sync_live_raises_when_email_missing(_isolate_home: Path) -> None:
    # Arrange — valid live cred but no ~/.claude.json email.
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": future_ms}})
    )
    # Act
    ctx = pytest.raises(LiveCredInvalidError)
    # Assert
    with ctx:
        sync_live(home=home)


# ---------------------------------------------------------------------------
# sync_live — identity guard (regression for the 2026-07 wrong-store clobber)
# ---------------------------------------------------------------------------


def _write_store_account_json(home: Path, slug: str, email: str) -> None:
    """Write a store's ``account.json`` recording its owning account email."""
    acct = home / ".scitex" / "agent-container" / "accounts" / slug
    acct.mkdir(parents=True, exist_ok=True)
    (acct / "account.json").write_text(
        json.dumps({"name": slug, "email_address": email})
    )


def test_sync_live_matched_identity_writes_correct_store(_isolate_home: Path) -> None:
    # Arrange — token identity agrees with the metadata email.
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    result = sync_live(home=home, identity_fn=lambda _p: "wyusuuke@gmail.com")
    # Assert
    assert result.store_name == "wyusuuke-gmail-com"


def test_sync_live_mismatched_identity_writes_token_store_not_metadata(
    _isolate_home: Path,
) -> None:
    # Arrange — stale metadata says wyusuuke, but the live token is ywatanabe.
    home = _isolate_home
    future_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", future_ms)
    # Act
    result = sync_live(home=home, identity_fn=lambda _p: "ywatanabe@scitex.ai")
    # Assert — the token's identity wins, not the stale metadata email.
    assert result.store_name == "ywatanabe-scitex-ai"


def test_sync_live_mismatched_identity_does_not_corrupt_wrong_store(
    _isolate_home: Path,
) -> None:
    # Arrange — a healthy wyusuuke store already exists; metadata is stale
    # (says wyusuuke) but the live token authenticates as ywatanabe.
    home = _isolate_home
    wyusuuke_ms = int((time.time() + 5_000) * 1_000)
    live_ms = int((time.time() + 3_600) * 1_000)
    wyusuuke_snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    wyusuuke_snap.parent.mkdir(parents=True, exist_ok=True)
    wyusuuke_snap.write_text(json.dumps({"claudeAiOauth": {"expiresAt": wyusuuke_ms}}))
    _write_live(home, "wyusuuke@gmail.com", live_ms)
    # Act
    sync_live(home=home, identity_fn=lambda _p: "ywatanabe@scitex.ai")
    # Assert — the wyusuuke store's credential is untouched (not clobbered).
    assert (
        json.loads(wyusuuke_snap.read_text())["claudeAiOauth"]["expiresAt"]
        == wyusuuke_ms
    )


def test_sync_live_mismatched_identity_writes_token_store_snapshot(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    live_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", live_ms)
    # Act
    sync_live(home=home, identity_fn=lambda _p: "ywatanabe@scitex.ai")
    # Assert — the token-identity store received the live credential.
    token_snap = _store_snapshot_path(home, "ywatanabe-scitex-ai")
    assert json.loads(token_snap.read_text())["claudeAiOauth"]["expiresAt"] == live_ms


def test_sync_live_offline_aborts_when_store_identity_differs(
    _isolate_home: Path,
) -> None:
    # Arrange — whoami offline (None); metadata points at the wyusuuke store,
    # but that store already records a DIFFERENT account (ywatanabe).
    home = _isolate_home
    live_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", live_ms)
    _write_store_account_json(home, "wyusuuke-gmail-com", "ywatanabe@scitex.ai")
    # Act
    ctx = pytest.raises(AccountIdentityError)
    # Assert
    with ctx:
        sync_live(home=home, identity_fn=lambda _p: None)


def test_sync_live_offline_abort_does_not_overwrite_snapshot(
    _isolate_home: Path,
) -> None:
    # Arrange — same offline mismatch, plus a pre-existing snapshot to protect.
    home = _isolate_home
    guarded_ms = int((time.time() + 5_000) * 1_000)
    live_ms = int((time.time() + 3_600) * 1_000)
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"claudeAiOauth": {"expiresAt": guarded_ms}}))
    _write_store_account_json(home, "wyusuuke-gmail-com", "ywatanabe@scitex.ai")
    _write_live(home, "wyusuuke@gmail.com", live_ms)
    # Act
    try:
        sync_live(home=home, identity_fn=lambda _p: None)
    except AccountIdentityError:
        pass
    # Assert — the guarded snapshot survived the aborted sync.
    assert json.loads(snap.read_text())["claudeAiOauth"]["expiresAt"] == guarded_ms


def test_sync_live_offline_saves_when_store_identity_matches(
    _isolate_home: Path,
) -> None:
    # Arrange — whoami offline, but the target store records the SAME account,
    # so the metadata fallback may safely save.
    home = _isolate_home
    live_ms = int((time.time() + 3_600) * 1_000)
    _write_live(home, "wyusuuke@gmail.com", live_ms)
    _write_store_account_json(home, "wyusuuke-gmail-com", "wyusuuke@gmail.com")
    # Act
    result = sync_live(home=home, identity_fn=lambda _p: None)
    # Assert
    assert result.action == "saved"


# ---------------------------------------------------------------------------
# account_freshness
# ---------------------------------------------------------------------------


def test_account_freshness_absent_when_no_snapshot(_isolate_home: Path) -> None:
    # Arrange — store dir has no snapshot for this account.
    home = _isolate_home
    # Act
    fresh = account_freshness("ghost", home=home)
    # Assert
    assert fresh.state == "ABSENT"


def test_account_freshness_valid_for_future_expiry(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    now = 1_700_000_000.0
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": int((now + 3_600) * 1_000)}})
    )
    # Act
    fresh = account_freshness("wyusuuke-gmail-com", home=home, now=now)
    # Assert
    assert fresh.state == "VALID"


def test_account_freshness_expired_for_past_expiry(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    now = 1_700_000_000.0
    snap = _store_snapshot_path(home, "wyusuuke-gmail-com")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": int((now - 3_600) * 1_000)}})
    )
    # Act
    fresh = account_freshness("wyusuuke-gmail-com", home=home, now=now)
    # Assert
    assert fresh.state == "EXPIRED"


def test_account_freshness_hours_signed_positive_when_valid(
    _isolate_home: Path,
) -> None:
    # Arrange — exactly 2h of life remaining.
    home = _isolate_home
    now = 1_700_000_000.0
    snap = _store_snapshot_path(home, "acct")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": int((now + 7_200) * 1_000)}})
    )
    # Act
    fresh = account_freshness("acct", home=home, now=now)
    # Assert
    assert fresh.hours == pytest.approx(2.0)


def test_freshness_label_renders_valid_with_signed_hours() -> None:
    # Arrange
    fresh = Freshness(state="VALID", hours=5.7)
    # Act
    label = fresh.label()
    # Assert
    assert label == "VALID (+5.7h)"


def test_freshness_label_renders_expired_with_negative_hours() -> None:
    # Arrange
    fresh = Freshness(state="EXPIRED", hours=-138.6)
    # Act
    label = fresh.label()
    # Assert
    assert label == "EXPIRED (-138.6h)"


def test_freshness_label_renders_absent_without_hours() -> None:
    # Arrange
    fresh = Freshness(state="ABSENT", hours=None)
    # Act
    label = fresh.label()
    # Assert
    assert label == "ABSENT"
