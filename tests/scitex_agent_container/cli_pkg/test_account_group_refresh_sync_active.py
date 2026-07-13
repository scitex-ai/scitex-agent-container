"""Tests for ``sac accounts refresh`` TTL gating + ``--sync-active-login``.

Fleet credential-rotation fix (2026-07): the host refresher must
(1) refresh every stored snapshot ONLY when its token is stale
(``--min-ttl-hours`` gate) and (2) when it rotates the account whose
snapshot refresh_token matches the LIVE ``~/.claude`` login, ALSO write
the freshly-rotated token block into ``~/.claude/.credentials.json`` so
the operator's live session is never stranded by the single-use
refresh_token rotation.

No-mocks (PA-306): real on-disk credentials + a real (redirected) HOME.
HTTP is injected at the ``urllib.request.urlopen`` production boundary;
the corrupt-write path is driven by save/restore-swapping the
``active_login_write._default_serialize`` production seam (the same
boundary-swap convention as the urlopen fixture — NOT the forbidden
``monkeypatch`` fixture). Tests NEVER touch a real ``~/.claude``.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account

_FUTURE_MS = 9_999_999_999_000  # far-future expiry (plenty of TTL left)
_PAST_MS = 1_000_000_000_000  # 2001 — long expired


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _seed_account(
    home: Path,
    name: str,
    *,
    refresh: str | None = "the-refresh",
    expires_ms: int | None = None,
    seed_file: bool = True,
) -> Path:
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = _store_dir(home) / name / ".credentials.json"
    if not seed_file:
        return creds
    oauth: dict[str, Any] = {"accessToken": "OLD-ACCESS", "clientId": "cid"}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if expires_ms is not None:
        oauth["expiresAt"] = expires_ms
    creds.write_text(json.dumps({"claudeAiOauth": oauth}))
    return creds


def _set_live(
    home: Path,
    *,
    refresh: str,
    access: str = "LIVE-OLD-ACCESS",
    extra: dict | None = None,
) -> Path:
    live = home / ".claude" / ".credentials.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": _FUTURE_MS,
        }
    }
    if extra:
        data.update(extra)
    live.write_text(json.dumps(data))
    return live


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


@pytest.fixture
def opener_swap() -> Iterator[dict]:
    """Swap ``urllib.request.urlopen`` at the production boundary.

    Default response rotates BOTH tokens so the snapshot's refresh_token
    is genuinely rotated (mirroring the real single-use behaviour).
    """
    import urllib.request

    state: dict[str, Any] = {
        "response": {
            "access_token": "NEW-ACCESS",
            "refresh_token": "NEW-REFRESH",
            "expires_in": 3600,
        }
    }
    saved = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        return _FakeResp(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        yield state
    finally:
        urllib.request.urlopen = saved  # type: ignore[assignment]


@pytest.fixture
def corrupt_serialize() -> Iterator[None]:
    """Save/restore-swap the live-login serializer to emit corrupt content.

    Same production-boundary-swap convention as ``opener_swap`` — exercises
    the verify-or-restore path end-to-end through the CLI without a mock.
    """
    from scitex_agent_container._account import active_login_write as alw

    saved = alw._default_serialize
    alw._default_serialize = lambda _d: "{}"  # valid JSON, tokens stripped
    try:
        yield
    finally:
        alw._default_serialize = saved


# ---------------------------------------------------------------------------
# TTL gating (--min-ttl-hours)
# ---------------------------------------------------------------------------


def test_all_skips_account_with_ample_ttl(sandbox_home, opener_swap) -> None:
    # Arrange — one account whose token is nowhere near expiry.
    creds = _seed_account(sandbox_home, "fresh", expires_ms=_FUTURE_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — a fresh token is left untouched (no rotation).
    assert json.loads(creds.read_text())["claudeAiOauth"]["accessToken"] == "OLD-ACCESS"


def test_all_refreshes_account_with_low_ttl(sandbox_home, opener_swap) -> None:
    # Arrange — one account whose token is already expired (TTL < threshold).
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert
    assert json.loads(creds.read_text())["claudeAiOauth"]["accessToken"] == "NEW-ACCESS"


def test_force_refreshes_fresh_account(sandbox_home, opener_swap) -> None:
    # Arrange — a fresh token that would otherwise be skipped.
    creds = _seed_account(sandbox_home, "fresh", expires_ms=_FUTURE_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--force"])
    # Assert — --force bypasses the TTL gate.
    assert json.loads(creds.read_text())["claudeAiOauth"]["accessToken"] == "NEW-ACCESS"


# ---------------------------------------------------------------------------
# Active-login sync (--sync-active-login)
# ---------------------------------------------------------------------------


def test_sync_writes_new_token_into_live_login(sandbox_home, opener_swap) -> None:
    # Arrange — the stored account shares the live login's refresh_token.
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    live = _set_live(sandbox_home, refresh="SHARED-REF")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — the live login received the freshly-rotated access token.
    got = json.loads(live.read_text())["claudeAiOauth"]["accessToken"]
    assert got == "NEW-ACCESS"


def test_sync_writes_rotated_refresh_token_into_live_login(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    live = _set_live(sandbox_home, refresh="SHARED-REF")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — the rotated refresh_token is what keeps the live login alive.
    got = json.loads(live.read_text())["claudeAiOauth"]["refreshToken"]
    assert got == "NEW-REFRESH"


def test_sync_preserves_other_live_keys(sandbox_home, opener_swap) -> None:
    # Arrange — the live file carries an unrelated key that must survive.
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    live = _set_live(
        sandbox_home, refresh="SHARED-REF", extra={"unrelatedKey": "keep-me"}
    )
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert
    assert json.loads(live.read_text())["unrelatedKey"] == "keep-me"


def test_sync_exits_zero_on_success(sandbox_home, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    _set_live(sandbox_home, refresh="SHARED-REF")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert
    assert result.exit_code == 0


def test_non_active_account_does_not_touch_live_login(
    sandbox_home, opener_swap
) -> None:
    # Arrange — the ONLY stored account does NOT match the live refresh_token.
    _seed_account(sandbox_home, "other", refresh="OTHER-REF")
    live = _set_live(sandbox_home, refresh="LIVE-REF", access="LIVE-OLD-ACCESS")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — the live login is left completely untouched.
    got = json.loads(live.read_text())["claudeAiOauth"]["accessToken"]
    assert got == "LIVE-OLD-ACCESS"


def test_sync_does_not_leak_token_values(sandbox_home, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    _set_live(sandbox_home, refresh="SHARED-REF")
    opener_swap["response"] = {
        "access_token": "SECRET-ACCESS-XYZ",
        "refresh_token": "SECRET-REFRESH-XYZ",
        "expires_in": 3600,
    }
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — no token bytes surface in the command output.
    leaks = ("SECRET-ACCESS-XYZ", "SECRET-REFRESH-XYZ", "SHARED-REF")
    assert not any(leak in (result.output or "") for leak in leaks)


# ---------------------------------------------------------------------------
# Verify-or-restore (corrupt live-login write)
# ---------------------------------------------------------------------------


def test_sync_verify_failure_exits_nonzero(
    sandbox_home, opener_swap, corrupt_serialize
) -> None:
    # Arrange — the live-login write will produce token-stripped JSON.
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    _set_live(sandbox_home, refresh="SHARED-REF")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — the failed sync fails the whole run loud.
    assert result.exit_code != 0


def test_sync_verify_failure_restores_live_login(
    sandbox_home, opener_swap, corrupt_serialize
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    live = _set_live(sandbox_home, refresh="SHARED-REF", access="LIVE-OLD-ACCESS")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--sync-active-login"])
    # Assert — the original live login is restored from the .bak backup.
    got = json.loads(live.read_text())["claudeAiOauth"]["accessToken"]
    assert got == "LIVE-OLD-ACCESS"


# ---------------------------------------------------------------------------
# Graceful skips
# ---------------------------------------------------------------------------


def test_missing_snapshot_does_not_block_other_accounts(
    sandbox_home, opener_swap
) -> None:
    # Arrange — 'gone' has account.json but NO credentials file; 'ok' is healthy.
    _seed_account(sandbox_home, "gone", seed_file=False)
    creds_ok = _seed_account(sandbox_home, "ok", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — the healthy account is still refreshed despite the missing one.
    assert json.loads(creds_ok.read_text())["claudeAiOauth"]["accessToken"] == "NEW-ACCESS"


def test_no_refresh_token_does_not_block_other_accounts(
    sandbox_home, opener_swap
) -> None:
    # Arrange — 'norefresh' lacks a refresh_token; 'ok' is healthy.
    _seed_account(sandbox_home, "norefresh", refresh=None, expires_ms=_PAST_MS)
    creds_ok = _seed_account(sandbox_home, "ok", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert
    assert json.loads(creds_ok.read_text())["claudeAiOauth"]["accessToken"] == "NEW-ACCESS"


def test_sync_active_login_rejects_skip_active(sandbox_home, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "work", refresh="SHARED-REF")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        account, ["refresh", "--all", "--sync-active-login", "--skip-active"]
    )
    # Assert — the two flags are mutually exclusive.
    assert result.exit_code != 0
