"""Tests for ``_creds._quota_rank`` (conditional ranking + fleet spread).

No mocks (PA-306): utilisation is injected through the documented
``usage_5h`` / ``usage_7d`` override mappings and, for the cache-read
helpers, a real JSON file under ``tmp_path``. AAA markers (TQ002),
descriptive names (TQ003), one assertion per test (TQ007).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scitex_agent_container._creds._quota_rank import (
    account_5h_usage,
    account_7d_reset_at,
    account_7d_usage,
    is_expiring_7d,
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
                    "alpha@example.com": {
                        "short": "alpha",
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
    pct = account_5h_usage("alpha-example-com", quota_cache_path=cache)
    # Assert
    assert pct == 100.0


def test_account_7d_usage_reads_d7_field_from_cache(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(tmp_path)
    # Act
    pct = account_7d_usage("alpha-example-com", quota_cache_path=cache)
    # Assert
    assert pct == 60.0


def test_account_5h_usage_returns_none_when_cache_missing(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "no-such-cache.json"
    # Act
    pct = account_5h_usage("alpha-example-com", quota_cache_path=missing)
    # Assert
    assert pct is None


def test_account_5h_usage_override_bypasses_cache(tmp_path: Path) -> None:
    # Arrange
    cache = _write_cache(tmp_path)  # says 100 — override must win
    # Act
    pct = account_5h_usage(
        "alpha-example-com",
        usage_5h={"alpha-example-com": 3.0},
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


# ---------------------------------------------------------------------------
# pick_ranked — EXPIRING capacity (use-it-or-lose-it)
#
# The operator's LIVE account table, 2026-07-14 (`sac accounts list`):
#
#   alpha-example-com   5h 28%   7d 67%  (resets in 5d 14h)
#   beta-example-com  5h  0%   7d 90%  (resets in 9h06m)
#   ywatanabe-scitex-ai  5h  0%   7d 90%  (resets in 6 MINUTES)
#
# ywatanabe-scitex-ai holds 10% of a 7-day Max-20x window that is about
# to be DELETED. The reset-blind ranker scored it identically to a 90%
# account with 6 days left, demoted both as "near-capped", and picked
# alpha — binning the 10%. 「毎回 90%で10%捨ててる」
# ---------------------------------------------------------------------------

_NOW = 1_780_000_000.0
_HOUR = 3600.0

_LIVE = ["alpha-example-com", "beta-example-com", "ywatanabe-scitex-ai"]
_LIVE_5H = {
    "alpha-example-com": 28.0,
    "beta-example-com": 0.0,
    "ywatanabe-scitex-ai": 0.0,
}
_LIVE_7D = {
    "alpha-example-com": 67.0,
    "beta-example-com": 90.0,
    "ywatanabe-scitex-ai": 90.0,
}
_LIVE_RESET = {
    "alpha-example-com": _NOW + 5 * 24 * _HOUR + 14 * _HOUR,  # 5d 14h
    "beta-example-com": _NOW + 9 * _HOUR + 6 * 60.0,  # 9h06m
    "ywatanabe-scitex-ai": _NOW + 6 * 60.0,  # 6 MINUTES
}


def test_pick_ranked_spends_the_7d_window_that_resets_in_minutes() -> None:
    # Arrange — the operator's live table (above), reset stamps included.
    # Act
    picked = pick_ranked(_LIVE, _LIVE_5H, _LIVE_7D, reset_7d=_LIVE_RESET, now=_NOW)
    # Assert — the 10% about to evaporate is SPENT, not binned.
    assert picked == "ywatanabe-scitex-ai"


def test_pick_ranked_bins_expiring_quota_when_reset_is_unknown() -> None:
    # Arrange — the SAME live table with no reset data (a quota cache
    # written before the populator persisted `reset_at_7d`). This pins the
    # pre-change behaviour: unknown reset must degrade EXACTLY to it, never
    # guess. It is also the bug the operator reported, in one line.
    # Act
    picked = pick_ranked(_LIVE, _LIVE_5H, _LIVE_7D, now=_NOW)
    # Assert
    assert picked == "alpha-example-com"


def test_pick_ranked_routes_fleet_to_the_expiring_account_on_the_spread_path() -> None:
    # Arrange — the SHIPPING path: `_start_preflight` always passes
    # spread_key=<agent name>, so this (not the legacy order) is what a real
    # boot takes. Before the fix the expiring account was demoted out of the
    # winning tier entirely and received EXACTLY ZERO agents.
    fleet = [f"agent-{i}" for i in range(60)]
    # Act
    picks = [
        pick_ranked(
            _LIVE, _LIVE_5H, _LIVE_7D, reset_7d=_LIVE_RESET, now=_NOW, spread_key=key
        )
        for key in fleet
    ]
    # Assert
    assert picks.count("ywatanabe-scitex-ai") > 0


def test_pick_ranked_spread_never_routes_to_the_far_out_reserve() -> None:
    # Arrange — beta is at 90% with 9h left: a genuine weekly reserve.
    # It must stay demoted even while the fleet drains the expiring account.
    fleet = [f"agent-{i}" for i in range(60)]
    # Act
    picks = [
        pick_ranked(
            _LIVE, _LIVE_5H, _LIVE_7D, reset_7d=_LIVE_RESET, now=_NOW, spread_key=key
        )
        for key in fleet
    ]
    # Assert
    assert picks.count("beta-example-com") == 0


def test_pick_ranked_still_avoids_a_near_capped_account_resetting_far_out() -> None:
    # Arrange — beta is ALSO at 90%, but its window has 9h left: a
    # genuine reserve, not expiring capacity. Drop the truly-expiring
    # account so the reserve is the only near-capped candidate left.
    names = ["alpha-example-com", "beta-example-com"]
    # Act
    picked = pick_ranked(names, _LIVE_5H, _LIVE_7D, reset_7d=_LIVE_RESET, now=_NOW)
    # Assert — avoiding a real reserve stays correct.
    assert picked == "alpha-example-com"


def test_pick_ranked_keeps_avoiding_expiring_account_blocked_on_5h() -> None:
    # Arrange — expiring 7d window, but the 5h wall is hit: it 429s NOW,
    # however soon its weekly budget refreshes. The immediate throttle
    # must still dominate.
    usage_5h = {"acct-expiring": 99.0, "acct-reserve": 10.0}
    usage_7d = {"acct-expiring": 90.0, "acct-reserve": 40.0}
    reset_7d = {"acct-expiring": _NOW + 300.0, "acct-reserve": _NOW + 6 * 24 * _HOUR}
    # Act
    picked = pick_ranked(
        ["acct-expiring", "acct-reserve"],
        usage_5h,
        usage_7d,
        reset_7d=reset_7d,
        now=_NOW,
    )
    # Assert
    assert picked == "acct-reserve"


def test_pick_ranked_ignores_expiring_account_with_no_headroom_left() -> None:
    # Arrange — 100% of the 7d window used and resetting in 5 minutes.
    # There is no capacity to reclaim; routing here would just 429.
    usage_7d = {"acct-exhausted": 100.0, "acct-reserve": 40.0}
    reset_7d = {"acct-exhausted": _NOW + 300.0, "acct-reserve": _NOW + 6 * 24 * _HOUR}
    # Act
    picked = pick_ranked(
        ["acct-exhausted", "acct-reserve"], {}, usage_7d, reset_7d=reset_7d, now=_NOW
    )
    # Assert
    assert picked == "acct-reserve"


def test_pick_ranked_treats_an_already_past_reset_stamp_as_unknown() -> None:
    # Arrange — a STALE cache: the stamp says the window reset already, so
    # the 90% reading cannot be trusted either. Degrade to avoidance rather
    # than route onto an account we can no longer reason about.
    usage_7d = {"acct-stale": 90.0, "acct-reserve": 40.0}
    reset_7d = {"acct-stale": _NOW - 600.0, "acct-reserve": _NOW + 6 * 24 * _HOUR}
    # Act
    picked = pick_ranked(
        ["acct-stale", "acct-reserve"], {}, usage_7d, reset_7d=reset_7d, now=_NOW
    )
    # Assert
    assert picked == "acct-reserve"


def test_pick_ranked_accepts_an_iso_reset_stamp_from_the_cache() -> None:
    # Arrange — the cache persists the upstream ISO-8601 string, not an
    # epoch, so the ranker must read that shape natively.
    usage_7d = {"acct-expiring": 90.0, "acct-reserve": 40.0}
    reset_7d = {
        "acct-expiring": "2026-06-01T00:30:00Z",
        "acct-reserve": "2026-06-08T00:00:00Z",
    }
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).timestamp()  # 30m before
    # Act
    picked = pick_ranked(
        ["acct-expiring", "acct-reserve"], {}, usage_7d, reset_7d=reset_7d, now=now
    )
    # Assert
    assert picked == "acct-expiring"


# ---------------------------------------------------------------------------
# pick_ranked — expiring capacity must NOT re-create the stacking incident
#
# Preferring expiring quota via a hard TIER would put EVERY booting agent
# on one account holding ~10% of a week. The preference is a spread WEIGHT
# instead, so the fleet drains it fast while still spreading.
# ---------------------------------------------------------------------------

_SPREAD_7D = {"acct-expiring": 90.0, "acct-reserve": 60.0}
_SPREAD_RESET = {
    "acct-expiring": _NOW + 10 * 60.0,
    "acct-reserve": _NOW + 5 * 24 * _HOUR,
}


def _spread_picks(n: int) -> list[str]:
    return [
        pick_ranked(
            ["acct-expiring", "acct-reserve"],
            {},
            _SPREAD_7D,
            reset_7d=_SPREAD_RESET,
            now=_NOW,
            spread_key=f"agent-{i}",
        )
        for i in range(n)
    ]


def test_pick_ranked_spread_drains_the_expiring_account_with_the_majority() -> None:
    # Arrange
    fleet_size = 100
    # Act
    picks = _spread_picks(fleet_size)
    # Assert — vanishing capacity is spent before the persisting reserve.
    assert picks.count("acct-expiring") > picks.count("acct-reserve")


def test_pick_ranked_spread_does_not_stack_the_whole_fleet_on_expiring() -> None:
    # Arrange
    fleet_size = 100
    # Act
    picks = _spread_picks(fleet_size)
    # Assert — the reserve still serves a real share: an account with 10%
    # of a week left must not absorb an entire fleet restart.
    assert picks.count("acct-reserve") > 0


# ---------------------------------------------------------------------------
# is_expiring_7d — the predicate itself
# ---------------------------------------------------------------------------


def test_is_expiring_7d_true_for_near_capped_window_resetting_in_minutes() -> None:
    # Arrange
    resets_in_6_minutes = _NOW + 6 * 60.0
    # Act
    verdict = is_expiring_7d(90.0, resets_in_6_minutes, _NOW)
    # Assert
    assert verdict is True


def test_is_expiring_7d_false_for_same_pct_resetting_days_out() -> None:
    # Arrange — 90% with 6 days left is a RESERVE, not expiring capacity.
    resets_in_6_days = _NOW + 6 * 24 * _HOUR
    # Act
    verdict = is_expiring_7d(90.0, resets_in_6_days, _NOW)
    # Assert
    assert verdict is False


def test_is_expiring_7d_false_when_reset_stamp_is_unknown() -> None:
    # Arrange
    reset_unknown = None
    # Act
    verdict = is_expiring_7d(90.0, reset_unknown, _NOW)
    # Assert
    assert verdict is False


def test_is_expiring_7d_false_when_no_headroom_remains() -> None:
    # Arrange — nothing left to reclaim; routing here only buys a 429.
    fully_used_pct = 100.0
    # Act
    verdict = is_expiring_7d(fully_used_pct, _NOW + 300.0, _NOW)
    # Assert
    assert verdict is False


# ---------------------------------------------------------------------------
# account_7d_reset_at — cache field reader
# ---------------------------------------------------------------------------


def test_account_7d_reset_at_reads_reset_at_7d_field_from_cache(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / "quota-cache.json"
    cache.write_text(
        json.dumps(
            {
                "written_at": 1.0,
                "accounts": {
                    "ywatanabe@scitex.ai": {
                        "short": "ywatanabe",
                        "h5": 0.0,
                        "d7": 90.0,
                        "ttl_h": 7.9,
                        "reset_at_7d": "2026-06-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    expected = datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()
    # Act
    reset_at = account_7d_reset_at("ywatanabe-scitex-ai", quota_cache_path=cache)
    # Assert
    assert reset_at == expected


def test_account_7d_reset_at_is_none_for_a_cache_without_the_field(
    tmp_path: Path,
) -> None:
    # Arrange — the pre-change cache shape (short/h5/d7/ttl_h only). It must
    # still parse; the picker just degrades to its reset-unaware ranking.
    cache = _write_cache(tmp_path)
    # Act
    reset_at = account_7d_reset_at("alpha-example-com", quota_cache_path=cache)
    # Assert
    assert reset_at is None
