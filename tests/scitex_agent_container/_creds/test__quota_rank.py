"""Tests for ``_creds._quota_rank`` (conditional ranking + fleet spread).

No mocks (PA-306): utilisation is injected through the documented
``usage_5h`` / ``usage_7d`` override mappings and, for the cache-read
helpers, a real JSON file under ``tmp_path``. AAA markers (TQ002),
descriptive names (TQ003), one assertion per test (TQ007).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._creds._quota_rank import (
    account_5h_usage,
    account_7d_usage,
    pick_ranked,
)

# ---------------------------------------------------------------------------
# account_5h_usage / account_7d_usage — cache field readers
# ---------------------------------------------------------------------------


def _write_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "quota-cache.json"
    cache.write_text(
        json.dumps(
            {
                "written_at": 1.0,
                "accounts": {
                    "wyusuuke@gmail.com": {
                        "short": "wyusuuke",
                        "h5": 100.0,
                        "d7": 60.0,
                        "ttl_h": 7.9,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return cache


def test_account_5h_usage_reads_h5_field_from_cache(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(tmp_path)
    # Act
    pct = account_5h_usage("wyusuuke-gmail-com", quota_cache_path=cache)
    # Assert
    assert pct == 100.0


def test_account_7d_usage_reads_d7_field_from_cache(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(tmp_path)
    # Act
    pct = account_7d_usage("wyusuuke-gmail-com", quota_cache_path=cache)
    # Assert
    assert pct == 60.0


def test_account_5h_usage_returns_none_when_cache_missing(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "no-such-cache.json"
    # Act
    pct = account_5h_usage("wyusuuke-gmail-com", quota_cache_path=missing)
    # Assert
    assert pct is None


def test_account_5h_usage_override_bypasses_cache(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(tmp_path)  # says 100 — override must win
    # Act
    pct = account_5h_usage(
        "wyusuuke-gmail-com",
        usage_5h={"wyusuuke-gmail-com": 3.0},
        quota_cache_path=cache,
    )
    # Assert
    assert pct == 3.0


# ---------------------------------------------------------------------------
# pick_ranked — conditional tiers
# ---------------------------------------------------------------------------

_ABC = ["acct-a", "acct-b", "acct-c"]


def test_pick_ranked_prefers_unblocked_over_5h_blocked() -> None:
    # Arrange — acct-a has the most 7d headroom but is at its 5h wall.
    usage_5h = {"acct-a": 100.0, "acct-b": 0.0, "acct-c": 0.0}
    usage_7d = {"acct-a": 2.0, "acct-b": 25.0, "acct-c": 60.0}
    # Act
    picked = pick_ranked(_ABC, usage_5h, usage_7d)
    # Assert — blocked-now loses to any unblocked candidate.
    assert picked == "acct-b"


def test_pick_ranked_prefers_headroom_over_near_capped() -> None:
    # Arrange — no 5h data at all (degrades to "not blocked").
    usage_7d = {"acct-a": 95.0, "acct-b": 40.0, "acct-c": 91.0}
    # Act
    picked = pick_ranked(_ABC, {}, usage_7d)
    # Assert
    assert picked == "acct-b"


def test_pick_ranked_known_7d_beats_unknown_within_a_tier() -> None:
    # Arrange — acct-a has no cache entry; a known-headroom account is
    # never displaced by a guess.
    usage_7d = {"acct-b": 40.0}
    # Act
    picked = pick_ranked(["acct-a", "acct-b"], {}, usage_7d)
    # Assert
    assert picked == "acct-b"


def test_pick_ranked_all_unknown_falls_back_to_candidate_order() -> None:
    # Arrange — no quota data anywhere: legacy freshness-only order.
    names = list(_ABC)
    # Act
    picked = pick_ranked(names, {}, {})
    # Assert
    assert picked == "acct-a"


def test_pick_ranked_ties_on_7d_break_by_lower_5h() -> None:
    # Arrange — equal 7d; the account with more 5h headroom wins.
    usage_5h = {"acct-a": 80.0, "acct-b": 10.0}
    usage_7d = {"acct-a": 30.0, "acct-b": 30.0}
    # Act
    picked = pick_ranked(["acct-a", "acct-b"], usage_5h, usage_7d)
    # Assert
    assert picked == "acct-b"


# ---------------------------------------------------------------------------
# pick_ranked — spread_key (weighted rendezvous hashing)
# ---------------------------------------------------------------------------


def test_pick_ranked_spread_is_stable_for_one_key() -> None:
    # Arrange
    usage_7d = {"acct-a": 25.0, "acct-b": 2.0}
    # Act
    picks = {
        pick_ranked(["acct-a", "acct-b"], {}, usage_7d, spread_key="agent-x")
        for _ in range(5)
    }
    # Assert — one key always maps to one account.
    assert len(picks) == 1


def test_pick_ranked_spread_covers_both_accounts_across_keys() -> None:
    # Arrange
    usage_7d = {"acct-a": 25.0, "acct-b": 2.0}
    # Act
    picks = {
        pick_ranked(["acct-a", "acct-b"], {}, usage_7d, spread_key=f"agent-{i}")
        for i in range(12)
    }
    # Assert — a fleet does not stack onto a single account.
    assert picks == {"acct-a", "acct-b"}


def test_pick_ranked_spread_weights_toward_more_7d_headroom() -> None:
    # Arrange — acct-b has ~10x the weekly headroom of acct-a; over a
    # large fleet it must serve the strict majority.
    usage_7d = {"acct-a": 90.0, "acct-b": 2.0}
    # Act
    picks = [
        pick_ranked(["acct-a", "acct-b"], {}, usage_7d, spread_key=f"agent-{i}")
        for i in range(100)
    ]
    # Assert
    assert picks.count("acct-b") > picks.count("acct-a")
