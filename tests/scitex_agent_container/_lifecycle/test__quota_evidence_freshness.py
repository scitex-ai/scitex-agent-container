"""A STALE quota snapshot is not evidence — it routes into a refresh.

MEASURED 2026-08-17, scitex-hub on scitex-compute-03. Its quota cache was
present, well-formed, and 23 hours old. The boot decision asked only "does a
cache exist", so the armed path trusted it and the picker read the pinned
account's previous-day percentages — 7d=15% — as evidence the pin was fine.
The account was at 7d=100%, capped until Aug 22. hub answered "You've hit
your weekly limit" on every turn while the restart reported success.
Refreshing that one cache revived it: the picker then chose a 7d=8% account
by itself. The selector was never wrong; it was fed a day-old number and had
no way to know.

This is the SECOND time that failure shape reached production — the module's
own docstring records scitex-02 on 2026-08-06, an agent booting onto a
d7=100% account while startup reported success. That fix armed the gate when
the cache was ABSENT. A STALE cache walks straight past an absence check,
which is why it recurred.

No mocks: each test writes a REAL cache file with a REAL `written_at` epoch
and reads it back through the production resolver. Age is supplied via the
``now`` seam rather than by sleeping.
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds import BlindQuotaCacheError
from scitex_agent_container._lifecycle._quota_evidence import (
    QUOTA_EVIDENCE_MAX_AGE_S,
    _has_fresh_quota_evidence,
)
from scitex_agent_container._lifecycle._start import _rotate_to_healthy_account
from scitex_agent_container.config import AgentConfig

_NOW = 1_786_000_000.0

# hub's pair. Distinct first dash-segments, because that segment is the quota
# cache's per-account match key (``short``).
_EXHAUSTED = "wyusuuke-gmail-com"
_HEALTHY = "ywatanabe-scitex-ai"

# Older than QUOTA_EVIDENCE_MAX_AGE_S by a wide margin, and the incident's own
# age. Written relative to the real clock because these boots go through the
# production reader, which has no ``now`` seam.
_A_DAY_OLD = 23 * 3600


def _write_cache(tmp_path, *, written_at, accounts=None):
    path = tmp_path / "quota-cache.json"
    path.write_text(
        json.dumps(
            {
                "written_at": written_at,
                "accounts": accounts
                if accounts is not None
                else {"acct": {"short": "a", "h5": 0.0, "d7": 15.0, "ttl_h": 6.0}},
            }
        )
    )
    return path


def test_a_cache_written_now_is_evidence(tmp_path):
    """Positive control: the check can return True at all."""
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is True


def test_a_day_old_cache_is_not_evidence(tmp_path):
    """hub's exact condition: present, well-formed, 23 hours old."""
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 23 * 3600)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_a_cache_just_inside_the_window_is_evidence(tmp_path):
    """Boundary, from the literal side — not computed from the constant.

    Deriving the boundary from QUOTA_EVIDENCE_MAX_AGE_S on both sides would
    keep passing if the constant were set to a year.
    """
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 3599)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is True


def test_a_cache_just_outside_the_window_is_not_evidence(tmp_path):
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 3601)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_an_absent_cache_is_not_evidence(tmp_path):
    # Arrange
    missing = tmp_path / "nope" / "quota-cache.json"
    # Act
    fresh = _has_fresh_quota_evidence(missing, now=_NOW)
    # Assert
    assert fresh is False


def test_an_undated_cache_is_not_evidence(tmp_path):
    """No `written_at` means the age is unknowable, and unknown is not OK."""
    # Arrange
    path = tmp_path / "quota-cache.json"
    path.write_text(json.dumps({"accounts": {"acct": {"h5": 0.0, "d7": 15.0}}}))
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_an_unparseable_cache_is_not_evidence(tmp_path):
    """A corrupt cache must fail toward refreshing, never toward launching."""
    # Arrange
    path = tmp_path / "quota-cache.json"
    path.write_text("{not json")
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


# ---------------------------------------------------------------------------
# End-to-end: a stale cache is REFRESHED, and the gate stays ARMED either way.
#
# PA-306: no mocks. The refresh is kept offline WITHOUT patching it —
# ``_account.claude_usage.fetch_usage_for_credentials`` consults its production
# per-account cache (``<account_dir>/usage.json``, 5-min TTL) before any token
# read or network call, so seeding that real file is enough to make the
# populator succeed, and a snapshot with no ``accessToken`` is enough to make
# it fail.
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _install_cache(tmp_path: Path, accounts: dict) -> Iterator[Path]:
    saved = os.environ.get("SAC_QUOTA_CACHE_PATH")
    cache = tmp_path / "runtime" / "quota-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"written_at": time.time() - _A_DAY_OLD, "accounts": accounts})
    )
    os.environ["SAC_QUOTA_CACHE_PATH"] = str(cache)
    try:
        yield cache
    finally:
        if saved is None:
            os.environ.pop("SAC_QUOTA_CACHE_PATH", None)
        else:
            os.environ["SAC_QUOTA_CACHE_PATH"] = saved


def _entry(short: str, *, h5: float, d7: float) -> dict:
    return {
        "short": short,
        "h5": h5,
        "d7": d7,
        "ttl_h": 6.0,
        "reset_at_5h": None,
        "reset_at_7d": None,
    }


@pytest.fixture
def _stale_inverted_cache(tmp_path: Path) -> Iterator[Path]:
    """A day-old cache whose numbers are the REVERSE of the current truth.

    Inverted rather than merely optimistic on purpose: a stale file that
    happened to agree with reality could not distinguish a build that
    re-measures from one that trusts it.
    """
    yield from _install_cache(
        tmp_path,
        {
            _EXHAUSTED: _entry("wyusuuke", h5=2.0, d7=5.0),
            _HEALTHY: _entry("ywatanabe", h5=40.0, d7=100.0),
        },
    )


@pytest.fixture
def _stale_blind_cache(tmp_path: Path) -> Iterator[Path]:
    """A day-old cache that exists and holds NOTHING."""
    yield from _install_cache(tmp_path, {})


def _default_store(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _write_snapshot(store: Path, slug: str, *, with_token: bool = True) -> Path:
    path = store / slug / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, object] = {"expiresAt": int((time.time() + 7_200.0) * 1_000)}
    if with_token:
        oauth["accessToken"] = "not-a-real-token"
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return path


def _seed_usage_cache(creds: Path, *, pct_5h: float, pct_7d: float) -> None:
    """Seed the REAL per-account usage cache the populator's fetcher reads."""
    (creds.parent / "usage.json").write_text(
        json.dumps(
            {
                "used_pct_5h": pct_5h,
                "used_pct_7d": pct_7d,
                "reset_at_5h": None,
                "reset_at_7d": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
        )
    )


def _pool_config(name: str, paths: list[Path]) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.credentials_files = [str(p) for p in paths]
    return cfg


def test_a_stale_cache_is_re_measured_and_its_wrong_numbers_are_not_used(
    _isolate_home, _stale_inverted_cache
):
    """hub's incident end-to-end: the stale numbers point the WRONG WAY.

    The cache on disk says the capped account is at d7=5% and the healthy one
    at d7=100% — an inversion, because that is what a day-old snapshot of a
    burning account looks like. The truth lives in each account's real
    ``usage.json``, which the populator reads.

    A build that trusts the stale file therefore picks the CAPPED account, and
    one that re-measures picks the healthy one. Asserting on the identity of
    the picked account rather than on "a refresh happened" means this cannot
    pass by observing the mechanism while the decision stays wrong.
    """
    # Arrange
    store = _default_store(_isolate_home)
    p_hot = _write_snapshot(store, _EXHAUSTED)
    p_cool = _write_snapshot(store, _HEALTHY)
    _seed_usage_cache(p_hot, pct_5h=12.0, pct_7d=100.0)
    _seed_usage_cache(p_cool, pct_5h=3.0, pct_7d=5.0)
    cfg = _pool_config("figrecipe", [p_hot, p_cool])

    # Act
    _rotate_to_healthy_account(cfg, log_stream=io.StringIO())

    # Assert
    assert cfg.claude.credentials_file == str(p_cool)


def test_a_stale_cache_does_not_disarm_the_fail_loud_gate(
    _isolate_home, _stale_blind_cache
):
    """The regression CI caught: staleness must never soften the refusal.

    A present-but-blind cache older than the window is still a host that RUNS
    a quota system, so the never-block invariant — which exists for hosts that
    run none — does not apply to it. Routing staleness into the absent-cache
    path made this boot DEGRADE and proceed, re-opening the 2026-07-20
    incident (scitex-cards launched onto a 7d=100% account read as "5h=? 7d=?")
    while fixing hub's.
    """
    # Arrange: a cache that is present, stale, and holds nothing — and no
    # credential the populator could use to clear the blindness.
    store = _default_store(_isolate_home)
    p_a = _write_snapshot(store, _EXHAUSTED, with_token=False)
    p_b = _write_snapshot(store, _HEALTHY, with_token=False)
    cfg = _pool_config("figrecipe", [p_a, p_b])

    # Act
    # Assert
    with pytest.raises(BlindQuotaCacheError):
        _rotate_to_healthy_account(cfg, log_stream=io.StringIO())


def test_the_window_is_not_generous_enough_to_hide_a_days_drift():
    """The constant must be well under the drift that caused the incident.

    hub's cache was 23h old. A window anywhere near that would have accepted
    it. Asserted against a literal so widening the constant to paper over a
    flaky refresher fails here rather than silently restoring the bug.
    """
    # Arrange
    a_day = 24 * 3600
    # Act
    window = QUOTA_EVIDENCE_MAX_AGE_S
    # Assert
    assert window <= a_day / 4
