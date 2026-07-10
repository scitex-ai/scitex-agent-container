"""Tests for openai_usage — spend ledger, price estimates, Costs API fetch.

No-mocks pattern (PA-306), mirroring ``test_claude_usage.py``: all HTTP
goes through the injected ``opener`` seam with hand-rolled callables
returning real ``urllib.response``-shaped objects; ledger/cache files
are real bytes on ``tmp_path``; env mutations use an explicit
save/restore fixture. STX-TQ002 AAA + STX-TQ007 one-assert-per-test.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._account import openai_usage as ou
from scitex_agent_container._account.openai_usage import (
    _check_no_key_leak,
    estimate_cost_usd,
    fetch_usage,
    read_spend,
    record_usage,
)

_ENV_KEYS = ("SAC_OPENAI_ADMIN_KEY", "OPENAI_ADMIN_KEY")


@pytest.fixture
def admin_env():
    """Scrub the admin-key env vars; restore on teardown."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Real fake response — the protocol urllib callers expect. NOT a mock.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _opener_returning(payload: Any):
    raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload

    def opener(req, timeout=None):
        return _FakeResp(raw)

    return opener


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


def _opener_sequence(*payloads: Any):
    state = {"i": 0}

    def opener(req, timeout=None):
        item = payloads[state["i"]]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return _FakeResp(json.dumps(item).encode())

    return opener


def _opener_must_not_be_called(req, timeout=None):
    raise AssertionError("opener was called when it must not be")


def _cost_bucket(start_ts: int, usd: float) -> dict[str, Any]:
    """One realistic Costs API bucket (shape per the org Costs endpoint)."""
    return {
        "object": "bucket",
        "start_time": start_ts,
        "end_time": start_ts + 86_400,
        "results": [
            {
                "object": "organization.costs.result",
                "amount": {"value": usd, "currency": "usd"},
                "line_item": None,
                "project_id": None,
            }
        ],
    }


def _costs_page(buckets: list[dict], has_more: bool = False, next_page=None):
    return {
        "object": "page",
        "data": buckets,
        "has_more": has_more,
        "next_page": next_page,
    }


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_known_model_prices_tokens():
    # Arrange
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    # Act
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    # Assert
    assert cost == pytest.approx(0.75)


def test_estimate_dated_snapshot_matches_family_prefix():
    # Arrange
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    # Act
    cost = estimate_cost_usd(usage, "gpt-5-mini-2026-01-01")
    # Assert
    assert cost == pytest.approx(0.25)


def test_estimate_longest_prefix_wins_over_shorter_family():
    # Arrange: "gpt-4.1-mini-x" must price as gpt-4.1-mini, not gpt-4.1.
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    # Act
    cost = estimate_cost_usd(usage, "gpt-4.1-mini-x")
    # Assert
    assert cost == pytest.approx(0.40)


def test_estimate_unknown_model_returns_none():
    # Arrange
    usage = {"input_tokens": 100, "output_tokens": 100}
    # Act
    cost = estimate_cost_usd(usage, "some-future-model")
    # Assert
    assert cost is None


def test_estimate_missing_token_counts_returns_none():
    # Arrange
    usage: dict[str, Any] = {}
    # Act
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    # Assert
    assert cost is None


# ---------------------------------------------------------------------------
# record_usage — the local spend ledger
# ---------------------------------------------------------------------------

_TURN = {"requests": 1, "input_tokens": 1_000_000, "output_tokens": 1_000_000}


def test_record_creates_the_ledger_file(tmp_path: Path):
    # Arrange
    ledger_path = tmp_path / ".scitex" / "cache" / "openai_spend.json"
    # Act
    record_usage(_TURN, model="gpt-4o-mini", home=tmp_path)
    # Assert
    assert ledger_path.is_file()


def test_record_returns_day_bucket_with_spend(tmp_path: Path):
    # Arrange
    # Act
    bucket = record_usage(_TURN, model="gpt-4o-mini", home=tmp_path)
    # Assert
    assert bucket["spend_usd"] == pytest.approx(0.75)


def test_record_accumulates_requests_across_turns(tmp_path: Path):
    # Arrange
    record_usage(_TURN, model="gpt-4o-mini", home=tmp_path)
    # Act
    bucket = record_usage(_TURN, model="gpt-4o-mini", home=tmp_path)
    # Assert
    assert bucket["requests"] == 2


def test_record_unknown_model_bumps_unpriced_turns(tmp_path: Path):
    # Arrange
    # Act
    bucket = record_usage(_TURN, model="mystery-model", home=tmp_path)
    # Assert
    assert bucket["unpriced_turns"] == 1


def test_record_tracks_per_agent_buckets(tmp_path: Path):
    # Arrange
    record_usage(_TURN, model="gpt-4o-mini", agent="alpha", home=tmp_path)
    ledger_path = tmp_path / ".scitex" / "cache" / "openai_spend.json"
    # Act
    ledger = json.loads(ledger_path.read_text())
    # Assert
    assert ledger["agents"]["alpha"]["spend_usd"] == pytest.approx(0.75)


def test_record_never_raises_on_unwritable_home():
    # Arrange
    unwritable = Path("/dev/null/not-a-dir")
    # Act
    bucket = record_usage(_TURN, model="gpt-4o-mini", home=unwritable)
    # Assert
    assert isinstance(bucket, dict)


# ---------------------------------------------------------------------------
# read_spend
# ---------------------------------------------------------------------------


def _write_ledger_days(home: Path, days: dict[str, float]) -> None:
    path = home / ".scitex" / "cache" / "openai_spend.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "days": {
            day: {
                "requests": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "spend_usd": usd,
                "unpriced_turns": 0,
            }
            for day, usd in days.items()
        }
    }
    path.write_text(json.dumps(payload))


def test_read_spend_without_ledger_reports_error(tmp_path: Path):
    # Arrange
    # Act
    summary = read_spend(home=tmp_path)
    # Assert
    assert summary["error"] == "no spend ledger recorded yet"


def test_read_spend_today_counts_todays_bucket(tmp_path: Path):
    # Arrange
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    _write_ledger_days(tmp_path, {"2026-07-10": 1.5})
    # Act
    summary = read_spend(home=tmp_path, now=now)
    # Assert
    assert summary["spend_usd_today"] == pytest.approx(1.5)


def test_read_spend_7d_includes_six_days_ago(tmp_path: Path):
    # Arrange
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    _write_ledger_days(tmp_path, {"2026-07-04": 2.0})
    # Act
    summary = read_spend(home=tmp_path, now=now)
    # Assert
    assert summary["spend_usd_7d"] == pytest.approx(2.0)


def test_read_spend_7d_excludes_eight_days_ago(tmp_path: Path):
    # Arrange
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    _write_ledger_days(tmp_path, {"2026-07-02": 2.0})
    # Act
    summary = read_spend(home=tmp_path, now=now)
    # Assert
    assert summary["spend_usd_7d"] == pytest.approx(0.0)


def test_read_spend_total_sums_all_days(tmp_path: Path):
    # Arrange
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    _write_ledger_days(tmp_path, {"2026-01-01": 1.0, "2026-07-10": 2.0})
    # Act
    summary = read_spend(home=tmp_path, now=now)
    # Assert
    assert summary["spend_usd_total"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# fetch_usage — Costs API (admin key), cache, never-raise
# ---------------------------------------------------------------------------


def test_fetch_without_admin_key_reports_actionable_error(
    tmp_path: Path, admin_env
):
    # Arrange
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_must_not_be_called)
    # Assert
    assert "SAC_OPENAI_ADMIN_KEY" in result["error"]


def test_fetch_cache_hit_skips_http(tmp_path: Path, admin_env):
    # Arrange
    cache_dir = tmp_path / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    cached = dict(ou._EMPTY_RESULT)
    cached["spend_usd_7d"] = 9.99
    cached["fetched_at"] = datetime.now(timezone.utc).isoformat()
    (cache_dir / "openai_usage.json").write_text(json.dumps(cached))
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_must_not_be_called)
    # Assert
    assert result["from_cache"] is True


def test_fetch_sums_bucket_spend_over_30d_window(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    now = int(time.time())
    page = _costs_page(
        [_cost_bucket(now - 2 * 86_400, 1.25), _cost_bucket(now - 86_400, 0.75)]
    )
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_returning(page))
    # Assert
    assert result["spend_usd_30d"] == pytest.approx(2.0)


def test_fetch_7d_window_excludes_old_buckets(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    now = int(time.time())
    page = _costs_page(
        [_cost_bucket(now - 10 * 86_400, 5.0), _cost_bucket(now - 86_400, 0.5)]
    )
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_returning(page))
    # Assert
    assert result["spend_usd_7d"] == pytest.approx(0.5)


def test_fetch_follows_pagination(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    now = int(time.time())
    page1 = _costs_page(
        [_cost_bucket(now - 86_400, 1.0)], has_more=True, next_page="page2"
    )
    page2 = _costs_page([_cost_bucket(now - 2 * 86_400, 2.0)])
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_sequence(page1, page2))
    # Assert
    assert result["spend_usd_30d"] == pytest.approx(3.0)


def test_fetch_http_error_is_reported_not_raised(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    http_401 = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_raising(http_401))
    # Assert
    assert "401" in result["error"]


def test_fetch_writes_the_cache_file(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    now = int(time.time())
    page = _costs_page([_cost_bucket(now - 86_400, 1.0)])
    # Act
    fetch_usage(home=tmp_path, opener=_opener_returning(page))
    # Assert
    assert (tmp_path / ".scitex" / "cache" / "openai_usage.json").is_file()


def test_fetch_result_carries_no_error_on_success(tmp_path: Path, admin_env):
    # Arrange
    os.environ["OPENAI_ADMIN_KEY"] = "sk-admin-fake"
    now = int(time.time())
    page = _costs_page([_cost_bucket(now - 86_400, 1.0)])
    # Act
    result = fetch_usage(home=tmp_path, opener=_opener_returning(page))
    # Assert
    assert result["error"] is None


# ---------------------------------------------------------------------------
# Key-leak guard
# ---------------------------------------------------------------------------


def test_leak_guard_rejects_secret_looking_value():
    # Arrange
    poisoned = {"spend_usd_7d": "sk-proj-abc123"}
    raised: Exception | None = None
    # Act
    try:
        _check_no_key_leak(poisoned)
    except RuntimeError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_leak_guard_rejects_secret_looking_key():
    # Arrange
    poisoned = {"admin_key_window": 1.0}
    raised: Exception | None = None
    # Act
    try:
        _check_no_key_leak(poisoned)
    except RuntimeError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_leak_guard_accepts_clean_spend_result():
    # Arrange
    clean = dict(ou._EMPTY_RESULT)
    # Act
    outcome = _check_no_key_leak(clean)
    # Assert
    assert outcome is None
