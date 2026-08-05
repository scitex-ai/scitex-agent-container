"""Tests for ``_creds._pick_healthy`` (CREDS-PHASE1 picker).

No mocks (PA-306): every test drives the real picker against real
JSON snapshots under a tmp store. AAA markers (TQ002), descriptive
names (TQ003), one assertion per test (TQ007).

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

from scitex_agent_container._creds._pick_healthy import (
    AccountHealth,
    NoHealthyAccountError,
    account_health,
    pick_healthy_account,
)


@pytest.fixture
def _isolate_home(tmp_path: Path):
    """Force ``Path.home()`` inside ``tmp_path`` for the test's duration.

    PA-306: no monkeypatch — ``Path.home()`` reads ``$HOME`` on Unix,
    so an explicit ``os.environ`` save/restore is the real equivalent.
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


@pytest.fixture(autouse=True)
def _isolate_quota_cache(tmp_path: Path):
    """Point the quota-cache reader at a nonexistent tmp file.

    Hermeticity fix (found during INCIDENT 2026-07-10 follow-up): agent
    containers bind the LIVE fleet ``/var/sac/quota-cache.json`` — the
    reader's DEFAULT path — so unpatched quota-aware pick tests read
    real production utilisation and flip winners depending on the
    fleet's current load. An explicitly-absent path degrades every
    lookup to ``None`` (freshness-only), the documented no-cache
    behavior the affected tests assume.
    """
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(tmp_path / "absent-quota-cache.json")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _store_root(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _write_snapshot(home: Path, name: str, expires_at_ms: int) -> Path:
    """Write a real per-account credential snapshot under the store."""
    path = _store_root(home) / name / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}}))
    return path


def _future_ms(seconds: float = 3600.0) -> int:
    return int((time.time() + seconds) * 1_000)


def _past_ms(seconds: float = 3600.0) -> int:
    return int((time.time() - seconds) * 1_000)


# ---------------------------------------------------------------------------
# account_health — building block (per-account state)
# ---------------------------------------------------------------------------


def test_account_health_returns_valid_for_unexpired_snapshot(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "alpha-example-com", _future_ms(7200))
    # Act
    h = account_health("alpha-example-com", home=home)
    # Assert
    assert h.state == "VALID"


def test_account_health_returns_expired_for_past_expiry(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "alpha-example-com", _past_ms(60))
    # Act
    h = account_health("alpha-example-com", home=home)
    # Assert
    assert h.state == "EXPIRED"


def test_account_health_returns_absent_when_snapshot_missing(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    # (no snapshot written)
    # Act
    h = account_health("beta-example-com", home=home)
    # Assert
    assert h.state == "ABSENT"


# ---------------------------------------------------------------------------
# pick_healthy_account — preferred wins when healthy
# ---------------------------------------------------------------------------


def test_pick_returns_preferred_when_preferred_is_healthy(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


# ---------------------------------------------------------------------------
# pick_healthy_account — rotation when preferred is unhealthy
# ---------------------------------------------------------------------------


def test_pick_rotates_to_healthy_when_preferred_is_expired(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(60))
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
    )
    # Assert
    assert picked == "alpha-example-com"


def test_pick_rotates_when_preferred_snapshot_absent(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # ywatanabe-scitex-ai snapshot deliberately missing
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
    )
    # Assert
    assert picked == "alpha-example-com"


def test_pick_falls_back_to_first_healthy_in_alphabetic_order(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred missing; two healthy candidates, ensure
    # deterministic pick (alphabetic).
    home = _isolate_home
    _write_snapshot(home, "alpha-example-com", _future_ms())
    _write_snapshot(home, "beta-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com", "beta-example-com"],
        home=home,
    )
    # Assert
    assert picked == "alpha-example-com"


# ---------------------------------------------------------------------------
# pick_healthy_account — preferred=None / "": still picks a healthy one
# ---------------------------------------------------------------------------


def test_pick_with_none_preferred_returns_first_healthy(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "beta-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=["ywatanabe-scitex-ai", "beta-example-com"],
        home=home,
    )
    # Assert
    assert picked == "beta-example-com"


# ---------------------------------------------------------------------------
# pick_healthy_account — fail-loud when nothing is healthy
# ---------------------------------------------------------------------------


def test_pick_raises_when_every_candidate_is_expired(_isolate_home: Path) -> None:
    # Arrange — all three accounts expired (the "all capped" worst case).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(120))
    _write_snapshot(home, "alpha-example-com", _past_ms(120))
    _write_snapshot(home, "beta-example-com", _past_ms(120))
    # Act
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert
    with ctx:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=[
                "ywatanabe-scitex-ai",
                "alpha-example-com",
                "beta-example-com",
            ],
            home=home,
        )


def test_pick_raises_when_candidate_list_is_empty(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    # Act
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert
    with ctx:
        pick_healthy_account("ywatanabe-scitex-ai", candidates=[], home=home)


def test_pick_error_message_names_every_candidate_state(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(60))
    _write_snapshot(home, "alpha-example-com", _past_ms(60))
    # Act — the message must let the operator see which accounts are
    # stale so they know which to `claude /login`.
    ctx = pytest.raises(NoHealthyAccountError, match=r"ywatanabe-scitex-ai")
    # Assert
    with ctx:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
            home=home,
        )


# ---------------------------------------------------------------------------
# pick_healthy_account — default candidate discovery (no explicit list)
# ---------------------------------------------------------------------------


def test_pick_discovers_candidates_from_store_when_unspecified(
    _isolate_home: Path,
) -> None:
    # Arrange — only one valid snapshot on disk; picker must auto-discover it.
    home = _isolate_home
    _write_snapshot(home, "beta-example-com", _future_ms())
    # Act
    picked = pick_healthy_account("ywatanabe-scitex-ai", home=home)
    # Assert
    assert picked == "beta-example-com"


# ---------------------------------------------------------------------------
# AccountHealth dataclass surface (used by callers that want to log)
# ---------------------------------------------------------------------------


def test_account_health_dataclass_carries_name_state_and_hours(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "alpha-example-com", _future_ms(3600))
    # Act
    h = account_health("alpha-example-com", home=home)
    # Assert
    assert isinstance(h, AccountHealth) and h.name == "alpha-example-com"


# ---------------------------------------------------------------------------
# pick_healthy_account — account-pool Phase 1: quota-aware headroom pick
#
# 7d utilisation is injected via the ``usage_7d`` override (name -> pct) so
# these tests need no real quota-cache.json and no network — mirroring the
# module's existing ``store_dir`` / ``home`` / ``now`` override idiom.
# ---------------------------------------------------------------------------


def test_pick_prefers_fresh_candidate_with_most_headroom(_isolate_home: Path) -> None:
    # Arrange — three fresh accounts; the middle one has the most 7d
    # headroom (lowest usage). No pinned preference.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    _write_snapshot(home, "beta-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=[
            "ywatanabe-scitex-ai",
            "alpha-example-com",
            "beta-example-com",
        ],
        home=home,
        usage_7d={
            "ywatanabe-scitex-ai": 50.0,
            "alpha-example-com": 10.0,
            "beta-example-com": 80.0,
        },
    )
    # Assert
    assert picked == "alpha-example-com"


def test_pick_avoids_near_capped_preferred_for_fresh_low_usage(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh but 95% capped; a fresh low-usage
    # alternative exists.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 95.0, "alpha-example-com": 12.0},
    )
    # Assert
    assert picked == "alpha-example-com"


def test_pick_keeps_preferred_when_fresh_and_has_headroom(_isolate_home: Path) -> None:
    # Arrange — preferred is fresh with headroom (40% < 90%). Even though
    # another fresh account has LOWER usage, churn is minimised: keep pref.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 40.0, "alpha-example-com": 10.0},
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


def test_pick_falls_back_to_freshness_only_when_quota_cache_absent(
    _isolate_home: Path,
) -> None:
    # Arrange — no injected usage AND a non-existent cache path: every
    # account's 7d% reads as unknown, so the pick degrades to the legacy
    # freshness-only behavior (first fresh candidate in order).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    missing_cache = home / "no-such-quota-cache.json"
    # Act
    picked = pick_healthy_account(
        None,
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_7d=None,
        quota_cache_path=missing_cache,
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


def test_pick_keeps_preferred_when_its_usage_unknown_despite_known_alt(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh but has NO cache entry (unknown 7d%);
    # a fresh alternative has a known low usage. Graceful degradation
    # keeps the preferred (freshness-only for that account), minimising
    # churn rather than rotating on incomplete data.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_7d={"alpha-example-com": 10.0},  # preferred deliberately absent
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


def test_pick_returns_least_used_fresh_when_all_near_capped(
    _isolate_home: Path,
) -> None:
    # Arrange — every fresh account is >= 90% (all near-capped). Headroom
    # is a PREFERENCE, not a hard gate: the picker returns the least-used
    # fresh account rather than raising (fail-loud is reserved for the
    # nothing-fresh case).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    _write_snapshot(home, "beta-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=[
            "ywatanabe-scitex-ai",
            "alpha-example-com",
            "beta-example-com",
        ],
        home=home,
        usage_7d={
            "ywatanabe-scitex-ai": 99.0,
            "alpha-example-com": 96.0,
            "beta-example-com": 91.0,
        },
    )
    # Assert
    assert picked == "beta-example-com"


def test_pick_ignores_quota_when_only_capped_account_is_fresh(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh but 98% capped; the ONLY alternative is
    # EXPIRED. A near-capped-but-fresh account must still win over a stale
    # token (freshness is the gate; quota only orders the fresh set).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _past_ms(120))
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 98.0, "alpha-example-com": 5.0},
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


# ---------------------------------------------------------------------------
# pick_healthy_account — account-pool Phase 2: the 5h axis ("blocked-now")
#
# 2026-07 incident: an account at 100% of its 5h window (429s immediately)
# was picked because its 7d % looked fine. The 5h axis is injected via the
# ``usage_5h`` override, mirroring ``usage_7d``.
# ---------------------------------------------------------------------------


def _write_three_fresh(home: Path) -> None:
    _write_snapshot(home, "alpha-example-com", _future_ms())
    _write_snapshot(home, "beta-example-com", _future_ms())
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())


_THREE = ["alpha-example-com", "beta-example-com", "ywatanabe-scitex-ai"]


def test_pick_rotates_off_preferred_at_5h_cap_despite_7d_headroom(
    _isolate_home: Path,
) -> None:
    # Arrange — the incident verbatim: preferred is token-fresh with 7d
    # headroom (60% < 90%) but sits at 100% of its 5h window; two
    # alternatives are idle. The preferred must be rotated off.
    home = _isolate_home
    _write_three_fresh(home)
    # Act
    picked = pick_healthy_account(
        "alpha-example-com",
        candidates=_THREE,
        home=home,
        usage_5h={
            "alpha-example-com": 100.0,
            "beta-example-com": 0.0,
            "ywatanabe-scitex-ai": 0.0,
        },
        usage_7d={
            "alpha-example-com": 60.0,
            "beta-example-com": 25.0,
            "ywatanabe-scitex-ai": 2.0,
        },
    )
    # Assert — no spread key → deterministic lowest-7d winner.
    assert picked == "ywatanabe-scitex-ai"


def test_pick_skips_5h_blocked_candidate_with_best_7d_headroom(
    _isolate_home: Path,
) -> None:
    # Arrange — no preference; the lowest-7d candidate is 5h-blocked, so
    # the next-lowest UNBLOCKED one must win (blocked-now beats headroom).
    home = _isolate_home
    _write_three_fresh(home)
    # Act
    picked = pick_healthy_account(
        None,
        candidates=_THREE,
        home=home,
        usage_5h={
            "alpha-example-com": 0.0,
            "beta-example-com": 0.0,
            "ywatanabe-scitex-ai": 97.0,
        },
        usage_7d={
            "alpha-example-com": 60.0,
            "beta-example-com": 25.0,
            "ywatanabe-scitex-ai": 2.0,
        },
    )
    # Assert
    assert picked == "beta-example-com"


def test_pick_returns_least_weekly_used_when_every_account_5h_blocked(
    _isolate_home: Path,
) -> None:
    # Arrange — the whole fleet is at its 5h wall. Blocked-now is a
    # preference, not a hard gate: the pick still returns (lowest 7d),
    # never raises.
    home = _isolate_home
    _write_three_fresh(home)
    # Act
    picked = pick_healthy_account(
        None,
        candidates=_THREE,
        home=home,
        usage_5h={
            "alpha-example-com": 100.0,
            "beta-example-com": 96.0,
            "ywatanabe-scitex-ai": 99.0,
        },
        usage_7d={
            "alpha-example-com": 60.0,
            "beta-example-com": 25.0,
            "ywatanabe-scitex-ai": 2.0,
        },
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


def test_pick_keeps_preferred_when_its_5h_usage_unknown(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh, 7d fine, and its 5h % is absent from
    # the cache. Unknown quota degrades to freshness-only for that
    # account: keep the preferred (only rotate on KNOWN bad quota).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "alpha-example-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "alpha-example-com"],
        home=home,
        usage_5h={"alpha-example-com": 0.0},  # preferred deliberately absent
        usage_7d={"ywatanabe-scitex-ai": 40.0, "alpha-example-com": 10.0},
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"


# ---------------------------------------------------------------------------
# pick_healthy_account — fleet load-balancing via spread_key
#
# A bulk restart must not stack every agent onto the same "best" account:
# with ``spread_key`` (the agent name) the winning tier is spread by
# 7d-headroom-weighted rendezvous hashing — deterministic per agent,
# different across agents.
# ---------------------------------------------------------------------------


def test_pick_with_spread_key_is_deterministic_per_agent(
    _isolate_home: Path,
) -> None:
    # Arrange — two eligible accounts; the same agent name must map to
    # the same account on every boot (no churn across restarts).
    home = _isolate_home
    _write_three_fresh(home)

    def _pick_once() -> str:
        return pick_healthy_account(
            None,
            candidates=_THREE,
            home=home,
            usage_5h={n: 0.0 for n in _THREE},
            usage_7d={
                "alpha-example-com": 95.0,
                "beta-example-com": 25.0,
                "ywatanabe-scitex-ai": 2.0,
            },
            spread_key="claude-code-telegrammer",
        )

    # Act
    first = _pick_once()
    second = _pick_once()
    # Assert
    assert first == second


def test_pick_spread_distributes_fleet_across_eligible_accounts(
    _isolate_home: Path,
) -> None:
    # Arrange — two accounts with comparable headroom; a 12-agent fleet
    # must land on BOTH (the incident: all agents stacked onto one).
    home = _isolate_home
    _write_three_fresh(home)
    usage_7d = {
        "alpha-example-com": 95.0,  # near-capped — out of the tier
        "beta-example-com": 25.0,
        "ywatanabe-scitex-ai": 2.0,
    }
    # Act
    picks = {
        pick_healthy_account(
            None,
            candidates=_THREE,
            home=home,
            usage_5h={n: 0.0 for n in _THREE},
            usage_7d=usage_7d,
            spread_key=f"agent-{i}",
        )
        for i in range(12)
    }
    # Assert
    assert picks == {"beta-example-com", "ywatanabe-scitex-ai"}


def test_pick_spread_never_selects_a_5h_blocked_account(
    _isolate_home: Path,
) -> None:
    # Arrange — one account is at its 5h wall; no agent in a 12-name
    # fleet may land on it while unblocked alternatives exist.
    home = _isolate_home
    _write_three_fresh(home)
    # Act
    picks = [
        pick_healthy_account(
            None,
            candidates=_THREE,
            home=home,
            usage_5h={
                "alpha-example-com": 100.0,
                "beta-example-com": 0.0,
                "ywatanabe-scitex-ai": 0.0,
            },
            usage_7d={
                "alpha-example-com": 60.0,
                "beta-example-com": 25.0,
                "ywatanabe-scitex-ai": 2.0,
            },
            spread_key=f"agent-{i}",
        )
        for i in range(12)
    ]
    # Assert
    assert "alpha-example-com" not in picks


# ---------------------------------------------------------------------------
# Blind-pick refusal — the remedy must name the CAUSE
#
# `require_quota_evidence=True` refuses when the cache says nothing about the
# account it picked. One remedy used to be named for two causes, and for one
# of them that command changes nothing:
#   * ZERO entries    — the populator never wrote one; a refresh needs stored
#                       accounts to refresh, so it can loop forever.
#   * entries, but    — genuinely stale for this fleet; a refresh IS the fix.
#     none for us
# A message identical for both inputs would be indistinguishable from no
# check at all, so the last test here asserts they differ.
#
# These pass `quota_cache_path=` explicitly and so are unaffected by the
# module's autouse `_isolate_quota_cache`, which only redirects the DEFAULT.
# ---------------------------------------------------------------------------

_BLIND_NOW = 1_000_000.0

#: An entry for an account NOT in the store — cache populated, just not with
#: anything covering this fleet.
_OTHER_FLEET = {
    "someone-else-com": {"short": "someone", "h5": 4.0, "d7": 2.0, "ttl_h": 6.0}
}


def _make_fresh_account(store: Path, slug: str) -> None:
    """A stored account whose OAuth snapshot is still valid at ``_BLIND_NOW``."""
    acct = store / slug
    acct.mkdir(parents=True, exist_ok=True)
    creds = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-FAKE-do-not-log",
            "refreshToken": "refresh-FAKE",
            "expiresAt": int((_BLIND_NOW + 8 * 3600.0) * 1000),
        }
    }
    (acct / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")


def _refuse(tmp_path: Path, cache_accounts: dict) -> str:
    """Drive the blind gate and return the refusal message."""
    store = tmp_path / "store"
    _make_fresh_account(store, "ywatanabe-scitex-ai")
    cache = tmp_path / "quota-cache.json"
    cache.write_text(
        json.dumps({"written_at": 1.0, "accounts": cache_accounts}), encoding="utf-8"
    )
    with pytest.raises(NoHealthyAccountError) as excinfo:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            store_dir=store,
            home=tmp_path,
            now=_BLIND_NOW,
            quota_cache_path=cache,
            require_quota_evidence=True,
        )
    return str(excinfo.value)


def test_empty_cache_refusal_names_sac_accounts_save(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the remedy a refresh cannot substitute for.
    assert "sac accounts save" in message


def test_empty_cache_refusal_says_zero_entries(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "ZERO account entries" in message


def test_empty_cache_refusal_warns_the_refresh_may_not_help(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the loop the old single-remedy text sent the operator into.
    assert "cannot help" in message


def test_stale_cache_refusal_names_the_refresh_command(tmp_path: Path) -> None:
    # Arrange
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "sac accounts refresh-quota-cache" in message


def test_stale_cache_refusal_does_not_send_the_operator_to_save(
    tmp_path: Path,
) -> None:
    # Arrange — accounts ARE stored here; saving another would be noise.
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "sac accounts save" not in message


def test_stale_cache_refusal_reports_how_many_entries_it_saw(tmp_path: Path) -> None:
    # Arrange
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the count is the evidence that the cache was actually read.
    assert "1 account entry" in message


def test_the_two_causes_do_not_produce_the_same_message(tmp_path: Path) -> None:
    # Arrange
    empty_dir = tmp_path / "empty"
    stale_dir = tmp_path / "stale"
    empty_dir.mkdir()
    stale_dir.mkdir()
    # Act
    empty_message = _refuse(empty_dir, {})
    stale_message = _refuse(stale_dir, dict(_OTHER_FLEET))
    # Assert — a check that says the same thing for every input checks nothing.
    assert empty_message != stale_message
