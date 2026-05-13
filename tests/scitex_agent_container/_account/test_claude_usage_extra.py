"""Extra coverage for _account.claude_usage targeting refresh + parse paths.

Hits the missing lines in the baseline coverage report:
    160-212 (_refresh_access_token), 228, 233-235, 240, 253-254 (cache),
    279, 287-288, 296, 299, 308, 314, 325, 341, 349, 400-403,
    411-425 (401 retry), 440-441.
"""

from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container._account import claude_usage as cu

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _home_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _make_home(tmp_path: Path, expires_at_ms: int | None = None) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    creds: dict[str, Any] = {
        "claudeAiOauth": {
            "accessToken": "old-access",
            "refreshToken": "fake-refresh",
            "clientId": "fake-client",
        }
    }
    if expires_at_ms is not None:
        creds["claudeAiOauth"]["expiresAt"] = expires_at_ms
    (home / ".claude" / ".credentials.json").write_text(json.dumps(creds))
    return home


def _mock_response(payload: Any) -> MagicMock:
    if isinstance(payload, (dict, list)):
        raw = json.dumps(payload).encode()
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raw = str(payload).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# _load_json edge cases
# ---------------------------------------------------------------------------


def test_load_json_returns_none_for_list_payload(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("[1, 2, 3]")
    assert cu._load_json(p) is None


def test_load_json_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert cu._load_json(tmp_path / "nope.json") is None


def test_load_json_returns_none_for_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("{nope")
    assert cu._load_json(p) is None


# ---------------------------------------------------------------------------
# _read_tokens corner cases
# ---------------------------------------------------------------------------


def test_read_tokens_missing_oauth_key(tmp_path: Path) -> None:
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    assert cu._read_tokens(home) == (None, None, None, None)


def test_read_tokens_oauth_not_a_dict(tmp_path: Path) -> None:
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": "string-value"})
    )
    assert cu._read_tokens(home) == (None, None, None, None)


def test_read_tokens_missing_creds_returns_all_none(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    assert cu._read_tokens(home) == (None, None, None, None)


# ---------------------------------------------------------------------------
# _is_token_expired
# ---------------------------------------------------------------------------


def test_is_token_expired_none_returns_false() -> None:
    assert cu._is_token_expired(None) is False


def test_is_token_expired_past_returns_true() -> None:
    assert cu._is_token_expired(0) is True


def test_is_token_expired_future_returns_false() -> None:
    far_future = int(time.time() * 1000) + 60 * 60 * 1000
    assert cu._is_token_expired(far_future) is False


# ---------------------------------------------------------------------------
# _refresh_access_token
# ---------------------------------------------------------------------------


def test_refresh_access_token_success_updates_creds(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=0)
    resp = _mock_response({"access_token": "NEW-TOKEN", "expires_in": 3600})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._refresh_access_token(home, "fake-refresh", "fake-client")
    assert out == "NEW-TOKEN"
    creds = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert creds["claudeAiOauth"]["accessToken"] == "NEW-TOKEN"
    assert creds["claudeAiOauth"]["expiresAt"] > int(time.time() * 1000)


def test_refresh_access_token_network_error_returns_none(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=0)
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        out = cu._refresh_access_token(home, "fake-refresh", "fake-client")
    assert out is None


def test_refresh_access_token_no_access_token_field(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=0)
    resp = _mock_response({"not_access_token": "x"})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._refresh_access_token(home, "fake-refresh", "fake-client")
    assert out is None


def test_refresh_access_token_creds_write_failure_still_returns_token(
    tmp_path: Path,
) -> None:
    """If credentials disk write fails, the new token is still returned."""
    home = _make_home(tmp_path, expires_at_ms=0)
    resp = _mock_response({"access_token": "NEW", "expires_in": 1000})
    # Make the credentials file unwritable by removing it after the refresh
    # response but before _refresh_access_token's open() — simulate by
    # patching open() to raise.
    import builtins

    real_open = builtins.open

    def _bad_open(path, *a, **kw):
        if str(path).endswith(".credentials.json") and "r+" in (
            a + (kw.get("mode", ""),) if a else (kw.get("mode", ""),)
        ):
            raise PermissionError("read-only fs")
        return real_open(path, *a, **kw)

    with (
        patch("urllib.request.urlopen", return_value=resp),
        patch("builtins.open", side_effect=_bad_open),
    ):
        out = cu._refresh_access_token(home, "fake-refresh", "fake-client")
    assert out == "NEW"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def test_read_cache_returns_none_when_missing_fetched_at(tmp_path: Path) -> None:
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "claude_usage.json").write_text(json.dumps({"x": 1}))
    assert cu._read_cache(home) is None


def test_read_cache_returns_none_for_bad_timestamp(tmp_path: Path) -> None:
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "claude_usage.json").write_text(
        json.dumps({"fetched_at": "not-a-timestamp"})
    )
    assert cu._read_cache(home) is None


def test_read_cache_returns_none_for_stale(tmp_path: Path) -> None:
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (cache_dir / "claude_usage.json").write_text(json.dumps({"fetched_at": stale}))
    assert cu._read_cache(home) is None


def test_read_cache_handles_naive_timestamp(tmp_path: Path) -> None:
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    naive = datetime.utcnow().isoformat()  # no tzinfo
    (cache_dir / "claude_usage.json").write_text(
        json.dumps({"fetched_at": naive, "used_tokens_5h": 5})
    )
    out = cu._read_cache(home)
    assert out is not None
    assert out["from_cache"] is True


def test_write_cache_handles_write_failure(tmp_path: Path) -> None:
    """If the cache write fails, _write_cache must not raise."""
    home = tmp_path / "h"
    home.mkdir()
    # Make the parent path point at an existing FILE so mkdir(parents=True)
    # raises NotADirectoryError.
    blocker = home / ".scitex"
    blocker.write_text("not a dir")
    cu._write_cache(home, {"ok": True})  # must not raise


# ---------------------------------------------------------------------------
# _fetch_from_api branches
# ---------------------------------------------------------------------------


def test_fetch_from_api_returns_list_payload() -> None:
    resp = _mock_response([{"window": "5h"}])
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out == [{"window": "5h"}]


def test_fetch_from_api_returns_wrapped_windows() -> None:
    resp = _mock_response({"windows": [{"window": "5h"}]})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out == [{"window": "5h"}]


def test_fetch_from_api_returns_wrapped_data() -> None:
    resp = _mock_response({"data": [{"window": "7d"}]})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out == [{"window": "7d"}]


def test_fetch_from_api_single_window_dict() -> None:
    resp = _mock_response({"window": "5h", "used": 1})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out == [{"window": "5h", "used": 1}]


def test_fetch_from_api_unknown_dict_returns_none() -> None:
    resp = _mock_response({"foo": "bar"})
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out is None


def test_fetch_from_api_bad_json_returns_none() -> None:
    resp = _mock_response(b"not json{")
    with patch("urllib.request.urlopen", return_value=resp):
        out = cu._fetch_from_api("tok")
    assert out is None


def test_fetch_from_api_http_401_reraises() -> None:
    err = urllib.error.HTTPError(
        url="http://x", code=401, msg="unauth", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError):
            cu._fetch_from_api("tok")


def test_fetch_from_api_http_500_returns_none() -> None:
    err = urllib.error.HTTPError(
        url="http://x", code=500, msg="oops", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert cu._fetch_from_api("tok") is None


# ---------------------------------------------------------------------------
# _parse_windows branches
# ---------------------------------------------------------------------------


def test_parse_windows_handles_non_int_used_limit() -> None:
    out = cu._parse_windows(
        [{"window": "5h", "used": "not-int", "limit": "x", "resetAt": 1}]
    )
    assert out["used_tokens_5h"] is None
    assert out["limit_tokens_5h"] is None
    assert out["used_pct_5h"] is None
    assert out["reset_at_5h"] is None  # int rejected


def test_parse_windows_zero_limit_gives_none_pct() -> None:
    out = cu._parse_windows([{"window": "5h", "used": 10, "limit": 0}])
    assert out["used_pct_5h"] is None


def test_parse_windows_skips_non_dict_and_unknown_window() -> None:
    out = cu._parse_windows(
        [
            "not-a-dict",
            {"window": "1d"},  # unknown
            {"window": "5h", "used": 1, "limit": 10},
        ]
    )
    assert out["used_pct_5h"] == 10.0


# ---------------------------------------------------------------------------
# _check_no_token_leak
# ---------------------------------------------------------------------------


def test_check_no_token_leak_raises_on_bad_key() -> None:
    with pytest.raises(RuntimeError, match="forbidden key"):
        cu._check_no_token_leak({"accessToken": "x"})


def test_check_no_token_leak_raises_on_bad_value() -> None:
    with pytest.raises(RuntimeError, match="forbidden value"):
        cu._check_no_token_leak({"note": "bearer foo"})


def test_check_no_token_leak_ignores_none_and_bool() -> None:
    cu._check_no_token_leak({"x": None, "y": True})  # must not raise


# ---------------------------------------------------------------------------
# fetch_usage — refresh path + 401 retry + leak short-circuit
# ---------------------------------------------------------------------------


def test_fetch_usage_refreshes_expired_token_then_succeeds(
    tmp_path: Path,
) -> None:
    home = _make_home(tmp_path, expires_at_ms=0)  # already expired

    refresh_calls: list[str] = []

    def fake_refresh(_home, _refresh, _client):
        refresh_calls.append("ok")
        return "NEW"

    api_payload = [
        {"window": "5h", "used": 100, "limit": 1000, "resetAt": "x"},
    ]

    with (
        patch.object(cu, "_refresh_access_token", side_effect=fake_refresh),
        patch("urllib.request.urlopen", return_value=_mock_response(api_payload)),
    ):
        result = cu.fetch_usage(home=home)

    assert refresh_calls == ["ok"]
    assert result["error"] is None
    assert result["used_tokens_5h"] == 100


def test_fetch_usage_handles_401_then_refresh_then_retry(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000)

    # First call raises 401, second call (after refresh) succeeds.
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=15):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                url="http://x", code=401, msg="", hdrs=None, fp=None
            )
        return _mock_response(
            [{"window": "7d", "used": 50, "limit": 500, "resetAt": "x"}]
        )

    with (
        patch.object(cu, "_refresh_access_token", return_value="REFRESHED"),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        result = cu.fetch_usage(home=home)
    assert result["error"] is None
    assert result["used_tokens_7d"] == 50


def test_fetch_usage_401_then_refresh_fails_returns_error(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000)

    def fake_urlopen(req, timeout=15):
        raise urllib.error.HTTPError(
            url="http://x", code=401, msg="", hdrs=None, fp=None
        )

    with (
        patch.object(cu, "_refresh_access_token", return_value=None),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        result = cu.fetch_usage(home=home)
    assert result["error"] is not None
    assert "HTTP 401" in result["error"]


def test_fetch_usage_unparsable_response_returns_error(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000)
    resp = _mock_response({"unexpected": "shape"})
    with patch("urllib.request.urlopen", return_value=resp):
        result = cu.fetch_usage(home=home)
    assert result["error"] is not None
    assert "Failed to fetch or parse" in result["error"]


def test_fetch_usage_writes_cache_on_success(tmp_path: Path) -> None:
    home = _make_home(tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000)
    resp = _mock_response([{"window": "5h", "used": 1, "limit": 10, "resetAt": "now"}])
    with patch("urllib.request.urlopen", return_value=resp):
        result = cu.fetch_usage(home=home)
    assert result["error"] is None
    assert (home / ".scitex" / "cache" / "claude_usage.json").is_file()
