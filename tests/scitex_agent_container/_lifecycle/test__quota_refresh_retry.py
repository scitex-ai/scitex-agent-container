"""Automatic one-shot quota refresh for a blind boot account pick."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scitex_agent_container._account.quota_cache_refresh import refresh_quota_cache
from scitex_agent_container._creds import POLICY_SPREAD, NoHealthyAccountError
from scitex_agent_container._lifecycle._quota_refresh_retry import (
    pick_boot_account,
)

_NOW = 1_000_000.0


def _write_snapshot(store: Path, slug: str, *, fresh: bool = True) -> None:
    account = store / slug
    account.mkdir(parents=True)
    expires_at = (
        int((_NOW + 8 * 3_600.0) * 1_000)
        if fresh
        else int(_NOW - 3_600.0)
    )
    payload = {
        "claudeAiOauth": {
            "accessToken": "fake-access-token",
            "refreshToken": "fake-refresh-token",
            "expiresAt": expires_at,
        }
    }
    (account / ".credentials.json").write_text(json.dumps(payload))


def _usage(_credentials_path: Path) -> dict:
    return {
        "used_pct_5h": 12.0,
        "used_pct_7d": 34.0,
        "reset_at_5h": None,
        "reset_at_7d": None,
    }


def test_blind_cache_is_refreshed_then_selection_succeeds(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "accounts"
    cache = tmp_path / "quota-cache.json"
    slug = "person-example-com"
    _write_snapshot(store, slug)
    cache.write_text(json.dumps({"written_at": _NOW, "accounts": {}}))
    log = io.StringIO()

    def refresher(**kwargs):
        return refresh_quota_cache(
            **kwargs,
            home=tmp_path,
            usage_fetcher=_usage,
            now=_NOW,
        )

    # Act
    picked = pick_boot_account(
        slug,
        candidates=[slug],
        store_dir=store,
        now=_NOW,
        quota_cache_path=cache,
        spread_key="agent-a",
        policy=POLICY_SPREAD,
        require_quota_evidence=True,
        log_stream=log,
        quota_refresher=refresher,
    )

    # Assert
    assert picked == slug


def test_successful_automatic_refresh_is_reported(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "accounts"
    cache = tmp_path / "quota-cache.json"
    slug = "person-example-com"
    _write_snapshot(store, slug)
    cache.write_text(json.dumps({"written_at": _NOW, "accounts": {}}))
    log = io.StringIO()

    def refresher(**kwargs):
        return refresh_quota_cache(
            **kwargs,
            home=tmp_path,
            usage_fetcher=_usage,
            now=_NOW,
        )

    # Act
    pick_boot_account(
        slug,
        candidates=[slug],
        store_dir=store,
        now=_NOW,
        quota_cache_path=cache,
        spread_key="agent-a",
        policy=POLICY_SPREAD,
        require_quota_evidence=True,
        log_stream=log,
        quota_refresher=refresher,
    )

    # Assert
    assert "automatic refresh succeeded for 1/1 account(s)" in log.getvalue()


def test_expired_credentials_do_not_trigger_quota_refresh(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "accounts"
    cache = tmp_path / "quota-cache.json"
    slug = "person-example-com"
    _write_snapshot(store, slug, fresh=False)
    cache.write_text(json.dumps({"written_at": _NOW, "accounts": {}}))
    calls = 0

    def refresher(**_kwargs):
        nonlocal calls
        calls += 1
        return {}

    # Act
    with pytest.raises(NoHealthyAccountError):
        pick_boot_account(
            slug,
            candidates=[slug],
            store_dir=store,
            now=_NOW,
            quota_cache_path=cache,
            spread_key="agent-a",
            policy=POLICY_SPREAD,
            require_quota_evidence=True,
            quota_refresher=refresher,
        )

    # Assert
    assert calls == 0


def test_failed_refresh_retries_only_once_and_remains_blocked(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "accounts"
    cache = tmp_path / "quota-cache.json"
    slug = "person-example-com"
    _write_snapshot(store, slug)
    cache.write_text(json.dumps({"written_at": _NOW, "accounts": {}}))
    calls = 0

    def refresher(**_kwargs):
        nonlocal calls
        calls += 1
        return {"accounts_found": 1, "ok": 0, "failed": 1}

    # Act
    with pytest.raises(NoHealthyAccountError):
        pick_boot_account(
            slug,
            candidates=[slug],
            store_dir=store,
            now=_NOW,
            quota_cache_path=cache,
            spread_key="agent-a",
            policy=POLICY_SPREAD,
            require_quota_evidence=True,
            quota_refresher=refresher,
        )

    # Assert
    assert calls == 1
