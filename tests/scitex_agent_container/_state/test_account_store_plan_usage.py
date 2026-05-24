"""Tests for ``account_store.read_account_plan`` /
``read_account_usage_cache`` — the OFFLINE plan/tier reader and the
CACHE-ONLY usage reader that enrich ``sac account list``.

No-mocks: every case writes real ``<acct>/.credentials.json`` /
``<acct>/usage.json`` snapshots under ``tmp_path`` and reads them back.
The autouse ``_isolate_home`` fixture pins ``$HOME`` to ``tmp_path`` so
the store-path cascade resolves under the sandbox.

TQ: module docstring (TQ001); AAA markers (TQ002); descriptive names
(TQ003); one assert per test (TQ007).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._state.account_store import (
    read_account_plan,
    read_account_usage_cache,
    save_account,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path):
    """Pin ``$HOME`` to ``tmp_path`` (PA-306: env save/restore, no mock)."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _write_snapshot_creds(
    home: Path, name: str, *, subscription: str, tier: str
) -> None:
    """Write a real saved-account ``.credentials.json`` with the two
    non-secret fields the plan reader whitelists."""
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    acct_dir = _store_dir(home) / name
    (acct_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "subscriptionType": subscription,
                    "rateLimitTier": tier,
                }
            }
        )
    )


# ---------------------------------------------------------------------------
# read_account_plan — offline plan/tier from the snapshot
# ---------------------------------------------------------------------------


def test_read_account_plan_derives_max_20x_label(tmp_path: Path) -> None:
    # Arrange
    _write_snapshot_creds(
        tmp_path, "alpha", subscription="max", tier="default_claude_max_20x"
    )
    # Act
    plan = read_account_plan("alpha", home=tmp_path)
    # Assert
    assert plan["plan_label"] == "Max 20x"


def test_read_account_plan_surfaces_raw_rate_limit_tier(tmp_path: Path) -> None:
    # Arrange
    _write_snapshot_creds(
        tmp_path, "beta", subscription="pro", tier="default_claude_pro"
    )
    # Act
    plan = read_account_plan("beta", home=tmp_path)
    # Assert
    assert plan["rate_limit_tier"] == "default_claude_pro"


def test_read_account_plan_missing_snapshot_returns_all_none(
    tmp_path: Path,
) -> None:
    # Arrange — account metadata saved but no .credentials.json snapshot.
    save_account("gamma", {"email_address": "g@x"}, home=tmp_path)
    # Act
    plan = read_account_plan("gamma", home=tmp_path)
    # Assert
    assert plan == {
        "subscription_type": None,
        "rate_limit_tier": None,
        "plan_label": None,
    }


def test_read_account_plan_unknown_tier_yields_none_label(tmp_path: Path) -> None:
    # Arrange — a tier string the mapping doesn't know must NOT be
    # silently bucketed; plan_label stays None, raw value preserved.
    _write_snapshot_creds(
        tmp_path, "delta", subscription="enterprise", tier="future_tier_x"
    )
    # Act
    plan = read_account_plan("delta", home=tmp_path)
    # Assert
    assert plan["plan_label"] is None


# ---------------------------------------------------------------------------
# read_account_usage_cache — cache-only (None when absent)
# ---------------------------------------------------------------------------


def test_read_account_usage_cache_absent_returns_none(tmp_path: Path) -> None:
    # Arrange — no usage.json written for this account.
    save_account("epsilon", {"email_address": "e@x"}, home=tmp_path)
    # Act
    usage = read_account_usage_cache("epsilon", home=tmp_path)
    # Assert
    assert usage is None


def test_read_account_usage_cache_returns_cached_snapshot(tmp_path: Path) -> None:
    # Arrange — a real per-account usage.json cache exists.
    save_account("zeta", {"email_address": "z@x"}, home=tmp_path)
    usage_file = _store_dir(tmp_path) / "zeta" / "usage.json"
    usage_file.write_text(
        json.dumps({"used_pct_5h": 42, "used_pct_7d": 7, "as_of": "2026-05-24"})
    )
    # Act
    usage = read_account_usage_cache("zeta", home=tmp_path)
    # Assert
    assert usage == {"used_pct_5h": 42, "used_pct_7d": 7, "as_of": "2026-05-24"}
