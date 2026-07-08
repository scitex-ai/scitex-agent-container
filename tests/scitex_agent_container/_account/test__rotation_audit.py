"""Tests for the credential rotation audit log.

No-mocks (PA-306): real bytes on tmp_path; the only injected seam is
``identity_fn`` (a plain callable test seam already used by
``sync_live``). Asserts the 5 mandated fields land, that NO full token
leaks into the JSONL, and that the file is append-only valid JSONL.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._account import _rotation_audit as ra
from scitex_agent_container._account.creds_sync import sync_live
from scitex_agent_container._state.account_store import switch_account

# A distinctive fake token used across leak tests — if any full token ever
# reaches the audit file, this exact string would appear.
_SECRET = "sk-ant-oat01-SUPERSECRETTOKENVALUE-do-not-log"
_REFRESH_SECRET = "sk-ant-ort01-REFRESHSECRETVALUE-do-not-log"


def _seed_creds(path: Path, *, access: str, refresh: str | None = None,
                expires_at_ms: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict = {"accessToken": access}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


# ---------------------------------------------------------------------------
# fingerprint_token — opaque, one-way, never contains the token.
# ---------------------------------------------------------------------------


def test_fingerprint_token_is_none_for_empty() -> None:
    # Arrange
    token = None
    # Act
    fp = ra.fingerprint_token(token)
    # Assert
    assert fp is None


def test_fingerprint_token_does_not_contain_the_token() -> None:
    # Arrange
    token = _SECRET
    # Act
    fp = ra.fingerprint_token(token)
    # Assert — the fingerprint never embeds any slice of the secret.
    assert fp is not None and _SECRET not in fp


def test_fingerprint_token_is_stable() -> None:
    # Arrange
    token = "abc"
    # Act
    first, second = ra.fingerprint_token(token), ra.fingerprint_token(token)
    # Assert — same token → same fingerprint (so FROM→TO rotation is visible).
    assert first == second


# ---------------------------------------------------------------------------
# log_rotation_event — the 5 mandated fields + host land in the record.
# ---------------------------------------------------------------------------


def test_log_rotation_event_writes_all_five_fields(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path
    # Act
    ra.log_rotation_event(
        store=store,
        event="refresh",
        from_account="me@example.com",
        to_account="me@example.com",
        reason="scheduled refresh timer",
    )
    # Assert — every mandated field present in the single record.
    line = (tmp_path / ra.AUDIT_FILENAME).read_text().strip()
    rec = json.loads(line)
    for field in ("timestamp_utc", "event", "from_account", "to_account",
                  "reason", "host"):
        assert field in rec


def test_log_rotation_event_records_event_and_reason(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path
    # Act
    ra.log_rotation_event(
        store=store, event="auto-rotate", from_account="a", to_account="b",
        reason="quota threshold 80% hit",
    )
    # Assert
    rec = json.loads((tmp_path / ra.AUDIT_FILENAME).read_text().strip())
    assert rec["event"] == "auto-rotate" and rec["reason"] == "quota threshold 80% hit"


def test_log_rotation_event_is_append_only_valid_jsonl(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path
    # Act — two rotations append two lines.
    ra.log_rotation_event(store=store, event="switch", from_account="a",
                          to_account="b", reason="first")
    ra.log_rotation_event(store=store, event="switch", from_account="b",
                          to_account="c", reason="second")
    # Assert — exactly two lines, each valid JSON, in order.
    lines = (tmp_path / ra.AUDIT_FILENAME).read_text().splitlines()
    assert [json.loads(x)["reason"] for x in lines] == ["first", "second"]


def test_log_rotation_event_records_opaque_fingerprints(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path
    # Act — pass fingerprints (never raw tokens).
    ra.log_rotation_event(
        store=store, event="refresh", from_account="a", to_account="a",
        reason="r",
        from_token_fp=ra.fingerprint_token("OLD"),
        to_token_fp=ra.fingerprint_token("NEW"),
    )
    # Assert — the FROM→TO token change is visible via distinct fingerprints.
    rec = json.loads((tmp_path / ra.AUDIT_FILENAME).read_text().strip())
    assert rec["from_token_fp"] != rec["to_token_fp"]


# ---------------------------------------------------------------------------
# switch_account — the real rotation site writes an audit record.
# ---------------------------------------------------------------------------


def test_switch_account_writes_audit_record(tmp_path: Path) -> None:
    # Arrange — a stored account with a credential snapshot.
    store = tmp_path / "store"
    home = tmp_path / "home"
    _seed_creds(store / "work" / ".credentials.json", access="WORKTOKEN")
    # Act
    result = switch_account("work", store_dir=store, home=home)
    # Assert — switch succeeded AND an audit record for event=switch landed.
    rec = json.loads((store / ra.AUDIT_FILENAME).read_text().strip())
    assert result["success"] and rec["event"] == "switch" and rec["to_account"] == "work"


def test_switch_account_audit_never_leaks_the_token(tmp_path: Path) -> None:
    # Arrange — seed a credential holding a distinctive secret token.
    store = tmp_path / "store"
    home = tmp_path / "home"
    _seed_creds(store / "work" / ".credentials.json", access=_SECRET,
                refresh=_REFRESH_SECRET)
    # Act
    switch_account("work", store_dir=store, home=home)
    # Assert — the full secret never appears anywhere in the audit file.
    audit_text = (store / ra.AUDIT_FILENAME).read_text()
    assert _SECRET not in audit_text and _REFRESH_SECRET not in audit_text


def test_switch_account_auto_rotate_event_is_recorded(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    home = tmp_path / "home"
    _seed_creds(store / "work" / ".credentials.json", access="WORKTOKEN")
    # Act — quota-watch passes event="auto-rotate".
    switch_account("work", store_dir=store, home=home, event="auto-rotate",
                   reason="quota threshold 80% hit", from_account="old@example.com")
    # Assert
    rec = json.loads((store / ra.AUDIT_FILENAME).read_text().strip())
    assert rec["event"] == "auto-rotate" and rec["from_account"] == "old@example.com"


# ---------------------------------------------------------------------------
# sync_live — snapshotting a live login into the store audits event=sync-live.
# ---------------------------------------------------------------------------


def test_sync_live_writes_sync_live_audit_record(tmp_path: Path) -> None:
    # Arrange — a VALID (future-expiry) live credential and an empty store.
    home = tmp_path / "home"
    store = tmp_path / "store"
    future_ms = 9_999_999_999_000  # far-future expiry
    _seed_creds(home / ".claude" / ".credentials.json", access=_SECRET,
                expires_at_ms=future_ms)
    # Act — inject a deterministic identity (real callable test seam).
    result = sync_live(home=home, store_dir=store,
                       identity_fn=lambda _p: "me@example.com")
    # Assert — the snapshot was saved AND a sync-live audit record landed.
    rec = json.loads((store / ra.AUDIT_FILENAME).read_text().strip())
    assert result.action == "saved" and rec["event"] == "sync-live"


def test_sync_live_audit_never_leaks_the_token(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "home"
    store = tmp_path / "store"
    future_ms = 9_999_999_999_000
    _seed_creds(home / ".claude" / ".credentials.json", access=_SECRET,
                refresh=_REFRESH_SECRET, expires_at_ms=future_ms)
    # Act
    sync_live(home=home, store_dir=store,
              identity_fn=lambda _p: "me@example.com")
    # Assert — no full token in the audit file.
    audit_text = (store / ra.AUDIT_FILENAME).read_text()
    assert _SECRET not in audit_text and _REFRESH_SECRET not in audit_text
