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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms(7200))
    # Act
    h = account_health("wyusuuke-gmail-com", home=home)
    # Assert
    assert h.state == "VALID"


def test_account_health_returns_expired_for_past_expiry(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _past_ms(60))
    # Act
    h = account_health("wyusuuke-gmail-com", home=home)
    # Assert
    assert h.state == "EXPIRED"


def test_account_health_returns_absent_when_snapshot_missing(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    # (no snapshot written)
    # Act
    h = account_health("ywata1989-gmail-com", home=home)
    # Assert
    assert h.state == "ABSENT"


# ---------------------------------------------------------------------------
# pick_healthy_account — preferred wins when healthy
# ---------------------------------------------------------------------------


def test_pick_returns_preferred_when_preferred_is_healthy(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


def test_pick_rotates_when_preferred_snapshot_absent(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # ywatanabe-scitex-ai snapshot deliberately missing
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


def test_pick_falls_back_to_first_healthy_in_alphabetic_order(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred missing; two healthy candidates, ensure
    # deterministic pick (alphabetic).
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com", "ywata1989-gmail-com"],
        home=home,
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


# ---------------------------------------------------------------------------
# pick_healthy_account — preferred=None / "": still picks a healthy one
# ---------------------------------------------------------------------------


def test_pick_with_none_preferred_returns_first_healthy(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=["ywatanabe-scitex-ai", "ywata1989-gmail-com"],
        home=home,
    )
    # Assert
    assert picked == "ywata1989-gmail-com"


# ---------------------------------------------------------------------------
# pick_healthy_account — fail-loud when nothing is healthy
# ---------------------------------------------------------------------------


def test_pick_raises_when_every_candidate_is_expired(_isolate_home: Path) -> None:
    # Arrange — all three accounts expired (the "all capped" worst case).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(120))
    _write_snapshot(home, "wyusuuke-gmail-com", _past_ms(120))
    _write_snapshot(home, "ywata1989-gmail-com", _past_ms(120))
    # Act
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert
    with ctx:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=[
                "ywatanabe-scitex-ai",
                "wyusuuke-gmail-com",
                "ywata1989-gmail-com",
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
    _write_snapshot(home, "wyusuuke-gmail-com", _past_ms(60))
    # Act — the message must let the operator see which accounts are
    # stale so they know which to `claude /login`.
    ctx = pytest.raises(NoHealthyAccountError, match=r"ywatanabe-scitex-ai")
    # Assert
    with ctx:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
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
    _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account("ywatanabe-scitex-ai", home=home)
    # Assert
    assert picked == "ywata1989-gmail-com"


# ---------------------------------------------------------------------------
# AccountHealth dataclass surface (used by callers that want to log)
# ---------------------------------------------------------------------------


def test_account_health_dataclass_carries_name_state_and_hours(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms(3600))
    # Act
    h = account_health("wyusuuke-gmail-com", home=home)
    # Assert
    assert isinstance(h, AccountHealth) and h.name == "wyusuuke-gmail-com"


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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=[
            "ywatanabe-scitex-ai",
            "wyusuuke-gmail-com",
            "ywata1989-gmail-com",
        ],
        home=home,
        usage_7d={
            "ywatanabe-scitex-ai": 50.0,
            "wyusuuke-gmail-com": 10.0,
            "ywata1989-gmail-com": 80.0,
        },
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


def test_pick_avoids_near_capped_preferred_for_fresh_low_usage(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh but 95% capped; a fresh low-usage
    # alternative exists.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 95.0, "wyusuuke-gmail-com": 12.0},
    )
    # Assert
    assert picked == "wyusuuke-gmail-com"


def test_pick_keeps_preferred_when_fresh_and_has_headroom(_isolate_home: Path) -> None:
    # Arrange — preferred is fresh with headroom (40% < 90%). Even though
    # another fresh account has LOWER usage, churn is minimised: keep pref.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 40.0, "wyusuuke-gmail-com": 10.0},
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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    missing_cache = home / "no-such-quota-cache.json"
    # Act
    picked = pick_healthy_account(
        None,
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        usage_7d={"wyusuuke-gmail-com": 10.0},  # preferred deliberately absent
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
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    _write_snapshot(home, "ywata1989-gmail-com", _future_ms())
    # Act
    picked = pick_healthy_account(
        None,
        candidates=[
            "ywatanabe-scitex-ai",
            "wyusuuke-gmail-com",
            "ywata1989-gmail-com",
        ],
        home=home,
        usage_7d={
            "ywatanabe-scitex-ai": 99.0,
            "wyusuuke-gmail-com": 96.0,
            "ywata1989-gmail-com": 91.0,
        },
    )
    # Assert
    assert picked == "ywata1989-gmail-com"


def test_pick_ignores_quota_when_only_capped_account_is_fresh(
    _isolate_home: Path,
) -> None:
    # Arrange — preferred is fresh but 98% capped; the ONLY alternative is
    # EXPIRED. A near-capped-but-fresh account must still win over a stale
    # token (freshness is the gate; quota only orders the fresh set).
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    _write_snapshot(home, "wyusuuke-gmail-com", _past_ms(120))
    # Act
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        usage_7d={"ywatanabe-scitex-ai": 98.0, "wyusuuke-gmail-com": 5.0},
    )
    # Assert
    assert picked == "ywatanabe-scitex-ai"
