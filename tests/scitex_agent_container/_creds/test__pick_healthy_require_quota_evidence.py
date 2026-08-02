"""Blind-quota fail-loud gate for ``_creds._pick_healthy`` (``require_quota_evidence``).

CONSTITUTION §2 — *unknown is a third state, never collapsed into "OK"*. The
``sac agents start`` boot preflight sets ``require_quota_evidence=True`` so a
fully-BLIND quota pick — the selected account has NEITHER a cached 5h NOR a
cached 7d reading — fails loud instead of booting an unverifiable, possibly
quota-exhausted account. 2026-07-20 incident: an empty quota cache read
"5h=? 7d=?" and launched scitex-cards on a 7d=100% account.

Default ``require_quota_evidence=False`` must preserve the pre-existing graceful
degradation (library / test callers, quota-cron-less hosts) — an unknown cache
never blocks a boot unless the caller opts in.

No mocks (PA-306): real JSON snapshots under a tmp store; quota is injected via
the ``usage_5h`` / ``usage_7d`` seams. AAA markers (TQ002); descriptive names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._creds._pick_healthy import (
    NoHealthyAccountError,
    pick_healthy_account,
)


@pytest.fixture
def _isolate_home(tmp_path: Path):
    """Force ``Path.home()`` inside ``tmp_path`` for the test's duration."""
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

    Agent containers bind the LIVE fleet ``/var/sac/quota-cache.json`` (the
    reader's default). An explicitly-absent override keeps these tests
    independent of real production utilisation; quota reaches the picker only
    through the ``usage_*`` injection seams below.
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


def _write_fresh_snapshot(home: Path, name: str, now: float) -> Path:
    """Write a token-fresh (future-expiry) credential snapshot for *name*."""
    path = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-not-real",
                    "refreshToken": "sk-ant-ort-not-real",
                    "expiresAt": int((now + 100_000) * 1_000),  # far future = VALID
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return path


def test_all_blind_and_required_raises_no_healthy_account_error(_isolate_home):
    # Arrange: two token-fresh accounts, but the quota cache knows NOTHING
    # about either (empty injection = every account reads 5h=? 7d=?).
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("wyusuuke-gmail-com", "ywatanabe-scitex-ai"):
        _write_fresh_snapshot(home, name, now)

    # Act
    # Assert: the boot gate refuses a fully-blind pick.
    with pytest.raises(NoHealthyAccountError):
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["wyusuuke-gmail-com", "ywatanabe-scitex-ai"],
            home=home,
            now=now,
            usage_5h={},
            usage_7d={},
            require_quota_evidence=True,
        )


def test_all_blind_raises_the_REPAIRABLE_subclass_so_the_caller_retries(
    _isolate_home,
):
    # Arrange: same fully-blind fleet as above. The caller
    # (_lifecycle/_start_preflight) auto-refreshes the quota cache and re-picks
    # ONLY when it catches BlindQuotaCacheError — so if this ever degrades to
    # the bare parent, the self-repair silently stops firing and the operator
    # is back to running `sac accounts refresh-quota-cache` by hand. The
    # isinstance tests elsewhere would NOT catch that: they pin the class
    # relationship, not what the picker actually raises.
    from scitex_agent_container._creds import BlindQuotaCacheError

    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("wyusuuke-gmail-com", "ywatanabe-scitex-ai"):
        _write_fresh_snapshot(home, name, now)

    # Act
    # Assert
    with pytest.raises(BlindQuotaCacheError):
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["wyusuuke-gmail-com", "ywatanabe-scitex-ai"],
            home=home,
            now=now,
            usage_5h={},
            usage_7d={},
            require_quota_evidence=True,
        )


def test_all_blind_without_requirement_returns_a_fresh_account(_isolate_home):
    # Arrange: identical all-blind fleet, but the caller does NOT opt in.
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("wyusuuke-gmail-com", "ywatanabe-scitex-ai"):
        _write_fresh_snapshot(home, name, now)

    # Act: default require_quota_evidence=False keeps graceful degradation.
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["wyusuuke-gmail-com", "ywatanabe-scitex-ai"],
        home=home,
        now=now,
        usage_5h={},
        usage_7d={},
    )

    # Assert: a fresh account is still returned — no fail-loud, no crash.
    assert picked in {"wyusuuke-gmail-com", "ywatanabe-scitex-ai"}


def test_known_headroom_account_wins_over_blind_preferred(_isolate_home):
    # Arrange: the pinned account is BLIND; a sibling has KNOWN headroom.
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("ywatanabe-scitex-ai", "wyusuuke-gmail-com"):
        _write_fresh_snapshot(home, name, now)

    # Act: under the gate a blind pin is rotated off in favour of the account
    # we can actually see is healthy.
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",  # blind (no injected quota)
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        now=now,
        usage_5h={"wyusuuke-gmail-com": 5.0},
        usage_7d={"wyusuuke-gmail-com": 5.0},
        require_quota_evidence=True,
    )

    # Assert: rotated to the known-headroom account, not the blind pin.
    assert picked == "wyusuuke-gmail-com"


def test_known_but_capped_fleet_returns_least_bad_not_raise(_isolate_home):
    # Arrange: every account's quota is KNOWN (not blind) but near-capped.
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("wyusuuke-gmail-com", "ywatanabe-scitex-ai"):
        _write_fresh_snapshot(home, name, now)

    # Act: the gate fires on BLIND, never on merely-busy — a fleet whose quota
    # is visible-but-high still returns the least-bad account.
    picked = pick_healthy_account(
        None,
        candidates=["wyusuuke-gmail-com", "ywatanabe-scitex-ai"],
        home=home,
        now=now,
        usage_5h={"wyusuuke-gmail-com": 10.0, "ywatanabe-scitex-ai": 10.0},
        usage_7d={"wyusuuke-gmail-com": 92.0, "ywatanabe-scitex-ai": 95.0},
        require_quota_evidence=True,
    )

    # Assert: no NoHealthyAccountError; a known account is returned.
    assert picked in {"wyusuuke-gmail-com", "ywatanabe-scitex-ai"}


def test_blind_pin_with_sighted_near_capped_siblings_boots_on_a_sighted_one(
    _isolate_home,
):
    # Arrange: the 2026-07-25 incident shape. The pinned account is BLIND
    # (cancelled — its usage fetch FAILED so the cache has no entry), and
    # every sighted sibling is near-capped (7d ≥ 90). The blind account's
    # tier (not blocked, not near-capped, d7-unknown) sorts AHEAD of the
    # sighted-but-capped tier, so without the sighted-pool restriction the
    # ranking hands the gate a blind winner and the boot is refused even
    # though a verifiable least-bad account exists.
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("wyusuuke-gmail-com", "ywata1989-gmail-com", "ywatanabe-scitex-ai"):
        _write_fresh_snapshot(home, name, now)

    # Act: under the gate, blind candidates must not displace sighted ones.
    picked = pick_healthy_account(
        "wyusuuke-gmail-com",  # blind pin (no cached quota at all)
        candidates=[
            "wyusuuke-gmail-com",
            "ywata1989-gmail-com",
            "ywatanabe-scitex-ai",
        ],
        home=home,
        now=now,
        usage_5h={"ywata1989-gmail-com": 0.0, "ywatanabe-scitex-ai": 1.0},
        usage_7d={"ywata1989-gmail-com": 100.0, "ywatanabe-scitex-ai": 90.0},
        require_quota_evidence=True,
    )

    # Assert: least-bad SIGHTED account (lowest 7d %), not a refusal.
    assert picked == "ywatanabe-scitex-ai"


def test_blind_pick_error_names_the_selected_account(_isolate_home):
    # Arrange: a single fresh account with no quota evidence.
    home = _isolate_home
    now = 1_784_530_000.0
    _write_fresh_snapshot(home, "ywatanabe-scitex-ai", now)

    # Act
    # Assert: the error identifies which account could not be verified.
    with pytest.raises(NoHealthyAccountError, match="ywatanabe-scitex-ai"):
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["ywatanabe-scitex-ai"],
            home=home,
            now=now,
            usage_5h={},
            usage_7d={},
            require_quota_evidence=True,
        )


def test_blind_pick_error_names_the_refresh_command(_isolate_home):
    # Arrange: a single fresh account with no quota evidence.
    home = _isolate_home
    now = 1_784_530_000.0
    _write_fresh_snapshot(home, "ywatanabe-scitex-ai", now)

    # Act
    # Assert: the error is actionable — it names the fix command to run.
    with pytest.raises(NoHealthyAccountError, match="refresh-quota-cache"):
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            candidates=["ywatanabe-scitex-ai"],
            home=home,
            now=now,
            usage_5h={},
            usage_7d={},
            require_quota_evidence=True,
        )


def test_known_headroom_preferred_is_kept_under_requirement(_isolate_home):
    # Arrange: the pinned account has KNOWN headroom on both axes.
    home = _isolate_home
    now = 1_784_530_000.0
    for name in ("ywatanabe-scitex-ai", "wyusuuke-gmail-com"):
        _write_fresh_snapshot(home, name, now)

    # Act: the gate must not over-fire — a visibly-healthy pin is kept.
    picked = pick_healthy_account(
        "ywatanabe-scitex-ai",
        candidates=["ywatanabe-scitex-ai", "wyusuuke-gmail-com"],
        home=home,
        now=now,
        usage_5h={"ywatanabe-scitex-ai": 10.0, "wyusuuke-gmail-com": 20.0},
        usage_7d={"ywatanabe-scitex-ai": 10.0, "wyusuuke-gmail-com": 20.0},
        require_quota_evidence=True,
    )

    # Assert: the known-good pin is retained.
    assert picked == "ywatanabe-scitex-ai"
