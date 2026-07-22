"""Tests for :mod:`scitex_agent_container._account.quota_cache_refresh`.

The populator that writes the aggregate ``quota-cache.json`` the quota-aware
boot picker reads. The usage-fetch boundary is INJECTED (a fake fetcher
returning canned 5h/7d values) so no test hits the network.

Coverage:
  * a successful run writes a well-formed cache that ``read_quota_entry``
    reads back (the round-trip contract with the picker/reader);
  * a per-account failure is isolated — the other accounts are still written
    and the cache is not corrupted;
  * the write is atomic (no ``.tmp`` residue, file parses);
  * an empty store is a distinct outcome that writes NOTHING — no file is
    created, a prior file keeps its ``written_at``, and the boot picker's
    cache-present gate stays disarmed;
  * a missing per-account snapshot is a recorded failure, not a crash;
  * merge preserves a prior good entry for an account that fails this round.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scitex_agent_container._account.quota_cache import read_quota_entry
from scitex_agent_container._account.quota_cache_refresh import refresh_quota_cache

# Deterministic clock so TTL math is exact.
_NOW = 1_000_000.0


def _make_account(store: Path, slug: str, *, ttl_hours: float = 7.5) -> None:
    """Create a stored-account dir with a snapshot whose token expires in
    ``ttl_hours`` from ``_NOW``."""
    acct = store / slug
    acct.mkdir(parents=True, exist_ok=True)
    expires_at_ms = int((_NOW + ttl_hours * 3600.0) * 1000)
    creds = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-FAKE-do-not-log",
            "refreshToken": "refresh-FAKE",
            "expiresAt": expires_at_ms,
        }
    }
    (acct / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")


def _fetcher(canned: dict[str, dict[str, Any]]):
    """Return a usage_fetcher keyed by the account slug (creds parent dir)."""

    def _fetch(credentials_path: Path) -> dict[str, Any]:
        slug = credentials_path.parent.name
        return canned[slug]

    return _fetch


def _ok(h5: float, d7: float) -> dict[str, Any]:
    return {"used_pct_5h": h5, "used_pct_7d": d7, "error": None}


# ---------------------------------------------------------------------------
# Happy path — round-trip through the reader
# ---------------------------------------------------------------------------


def test_refresh_writes_entry_read_back_by_reader(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "ywatanabe-scitex-ai", ttl_hours=7.5)
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"ywatanabe-scitex-ai": _ok(19.0, 3.0)})
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="ywatanabe-scitex-ai", cache_path=cache)
    # Assert
    assert entry is not None and entry["short"] == "ywatanabe"


def test_refresh_entry_carries_h5_d7_and_ttl(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "ywatanabe-scitex-ai", ttl_hours=7.5)
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"ywatanabe-scitex-ai": _ok(19.0, 3.0)})
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="ywatanabe-scitex-ai", cache_path=cache)
    # Assert — the reset stamps are part of the entry shape now (None here:
    # this fetcher's response omits them, which must stay a usable entry).
    assert entry == {
        "short": "ywatanabe",
        "h5": 19.0,
        "d7": 3.0,
        "ttl_h": 7.5,
        "reset_at_5h": None,
        "reset_at_7d": None,
    }


def test_refresh_persists_the_7d_reset_stamp_for_the_picker(tmp_path: Path) -> None:
    # Arrange — the usage API returns `resets_at` for both windows and
    # `claude_usage` already parses it into reset_at_5h / reset_at_7d. The
    # populator used to DROP it here, which left the account picker unable
    # to tell expiring quota from a reserve (it binned ~10% of every 7d
    # window). It must round-trip into the cache the picker reads.
    store = tmp_path / "store"
    _make_account(store, "ywatanabe-scitex-ai", ttl_hours=7.5)
    cache = tmp_path / "quota-cache.json"
    usage = _ok(0.0, 90.0)
    usage["reset_at_7d"] = "2026-07-14T09:00:00+00:00"
    fetch = _fetcher({"ywatanabe-scitex-ai": usage})
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="ywatanabe-scitex-ai", cache_path=cache)
    # Assert
    assert entry is not None and entry["reset_at_7d"] == "2026-07-14T09:00:00+00:00"


def test_refresh_keeps_entry_usable_when_upstream_omits_reset(tmp_path: Path) -> None:
    # Arrange — the reset stamps are OPTIONAL, unlike h5/d7/ttl_h: a response
    # without them must still yield a cached entry (the picker just degrades
    # to its reset-unaware ranking), never blank the account.
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com", ttl_hours=3.0)
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"a-gmail-com": _ok(5.0, 1.0)})
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert result["ok"] == 1


def test_refresh_reports_ok_count(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    _make_account(store, "b-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"a-gmail-com": _ok(10.0, 1.0), "b-gmail-com": _ok(20.0, 2.0)})
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert result["ok"] == 2 and result["failed"] == 0


# ---------------------------------------------------------------------------
# Per-account failure isolation
# ---------------------------------------------------------------------------


def test_per_account_failure_does_not_block_others(tmp_path: Path) -> None:
    # Arrange — account b's fetch errors; a must still be written.
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    _make_account(store, "b-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher(
        {
            "a-gmail-com": _ok(10.0, 1.0),
            "b-gmail-com": {
                "used_pct_5h": None,
                "used_pct_7d": None,
                "error": "HTTP 429",
            },
        }
    )
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="a-gmail-com", cache_path=cache)
    # Assert
    assert entry is not None and entry["h5"] == 10.0


def test_per_account_failure_is_counted(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    _make_account(store, "b-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher(
        {
            "a-gmail-com": _ok(10.0, 1.0),
            "b-gmail-com": {
                "used_pct_5h": None,
                "used_pct_7d": None,
                "error": "HTTP 429",
            },
        }
    )
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert result["ok"] == 1 and result["failed"] == 1


def test_failed_account_absent_from_cache_when_no_prior(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "b-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher(
        {"b-gmail-com": {"used_pct_5h": None, "used_pct_7d": None, "error": "boom"}}
    )
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="b-gmail-com", cache_path=cache)
    # Assert
    assert entry is None


def test_missing_snapshot_is_recorded_failure(tmp_path: Path) -> None:
    # Arrange — account dir exists (metadata only) but no .credentials.json.
    store = tmp_path / "store"
    (store / "a-gmail-com").mkdir(parents=True)
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({})  # never called — snapshot missing short-circuits
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert (
        result["failed"] == 1
        and "no credentials snapshot" in result["results"][0]["error"]
    )


# ---------------------------------------------------------------------------
# Atomic write + empty store
# ---------------------------------------------------------------------------


def test_write_is_atomic_no_tmp_residue(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"a-gmail-com": _ok(10.0, 1.0)})
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert — the tmp sidecar was renamed away; only the final file remains.
    assert cache.is_file() and not Path(str(cache) + ".tmp").exists()


def test_written_file_has_accounts_and_written_at(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"a-gmail-com": _ok(10.0, 1.0)})
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    parsed = json.loads(cache.read_text(encoding="utf-8"))
    # Assert
    assert parsed["written_at"] == _NOW and "a-gmail-com" in parsed["accounts"]


def test_empty_store_does_not_crash(tmp_path: Path) -> None:
    # Arrange — store dir exists but holds no accounts.
    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    # Assert
    assert result["ok"] == 0 and result["failed"] == 0


# ---------------------------------------------------------------------------
# Zero accounts — a THIRD outcome, and it writes nothing
#
# The merge below preserves entries for accounts that FAIL. With no accounts
# there are no failures, so that guard covers none of this: an empty store
# used to write anyway.
# ---------------------------------------------------------------------------


def test_empty_store_reports_no_accounts_reason(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    # Assert — distinguishable from a real refresh without parsing prose.
    assert result["reason"] == "no-accounts"


def test_empty_store_reports_not_written(tmp_path: Path) -> None:
    # Arrange
    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    # Assert
    assert result["written"] is False


def test_empty_store_creates_no_cache_file(tmp_path: Path) -> None:
    # Arrange — no cache exists yet. Creating an empty one here is what makes
    # `quota_cache_present` report True, which arms the boot picker's
    # require_quota_evidence gate and converts graceful degradation into a
    # hard refusal. The file must simply not appear.
    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    # Assert
    assert not cache.exists()


def test_empty_store_does_not_arm_the_boot_quota_gate(tmp_path: Path) -> None:
    # Arrange — the consequence the file-absence test above exists to prevent,
    # asserted against the predicate the boot gate actually reads.
    from scitex_agent_container._account.quota_cache import quota_cache_present

    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    # Assert
    assert quota_cache_present(cache) is False


def test_empty_store_leaves_prior_written_at_untouched(tmp_path: Path) -> None:
    # Arrange — a prior cache exists. Rewriting it restamps `written_at` on
    # data nothing re-measured: a stale cache that now claims to be current.
    store = tmp_path / "store"
    store.mkdir()
    cache = tmp_path / "quota-cache.json"
    cache.write_text(
        json.dumps(
            {
                "written_at": 1.0,
                "accounts": {
                    "a-gmail-com": {"short": "a", "h5": 5.0, "d7": 1.0, "ttl_h": 3.0}
                },
            }
        ),
        encoding="utf-8",
    )
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=_fetcher({}),
        now=_NOW,
    )
    parsed = json.loads(cache.read_text(encoding="utf-8"))
    # Assert
    assert parsed["written_at"] == 1.0


def test_populated_store_still_reports_written_and_no_reason(tmp_path: Path) -> None:
    # Arrange — the discriminating counter-input: same call, one account.
    # Without this the "no-accounts" assertions above could pass on a
    # populator that had simply stopped writing altogether.
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher({"a-gmail-com": _ok(10.0, 1.0)})
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert result["written"] is True and result["reason"] is None


def test_all_accounts_failing_still_writes_the_merged_cache(tmp_path: Path) -> None:
    # Arrange — "every account failed" must stay a WRITE (the merge is what
    # preserves prior good data); only "no accounts at all" skips it.
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    cache = tmp_path / "quota-cache.json"
    fetch = _fetcher(
        {"a-gmail-com": {"used_pct_5h": None, "used_pct_7d": None, "error": "boom"}}
    )
    # Act
    result = refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    # Assert
    assert result["written"] is True and cache.is_file()


# ---------------------------------------------------------------------------
# Merge — prior good data survives a transient failure
# ---------------------------------------------------------------------------


def test_merge_preserves_prior_entry_on_failure(tmp_path: Path) -> None:
    # Arrange — seed a cache with a good entry, then a run where that
    # account's fetch fails. The prior entry must survive.
    store = tmp_path / "store"
    _make_account(store, "a-gmail-com")
    cache = tmp_path / "quota-cache.json"
    cache.write_text(
        json.dumps(
            {
                "written_at": 1.0,
                "accounts": {
                    "a-gmail-com": {"short": "a", "h5": 5.0, "d7": 1.0, "ttl_h": 3.0}
                },
            }
        ),
        encoding="utf-8",
    )
    fetch = _fetcher(
        {"a-gmail-com": {"used_pct_5h": None, "used_pct_7d": None, "error": "boom"}}
    )
    # Act
    refresh_quota_cache(
        home=tmp_path,
        store_dir=store,
        cache_path=cache,
        usage_fetcher=fetch,
        now=_NOW,
    )
    entry = read_quota_entry(account="a-gmail-com", cache_path=cache)
    # Assert — the stale-but-good prior entry is still there.
    assert entry is not None and entry["h5"] == 5.0
