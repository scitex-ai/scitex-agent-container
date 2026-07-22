"""Populate the aggregate ``quota-cache.json`` the quota-aware picker reads.

The boot-time account picker (:mod:`_creds._pick_healthy`) and the a2a
metadata enricher both read per-account 5h/7d utilisation + token TTL from an
aggregate ``quota-cache.json`` via :func:`_account.quota_cache.read_quota_entry`.
Nothing in the repo WROTE that file — it was documented as "the host-cron
schema" but no code produced it, so every reader saw "unknown" and the
quota-awareness was inert.

This module closes that gap. :func:`refresh_quota_cache` walks every stored
account (the same store :func:`_creds._pick_healthy.pick_healthy_account`
discovers), fetches each one's live 5h/7d utilisation from the Anthropic usage
API via the EXISTING :func:`_account.claude_usage.fetch_usage_for_credentials`
(reusing its per-account credential swap + 5-min cache — no reinvented API
call), reads the token TTL from the same snapshot, and writes the aggregate
cache through :func:`_account.quota_cache.write_quota_cache`.

Fail-loud, per account
----------------------
A single account's fetch failure (network error, lapsed refresh_token,
missing snapshot) is recorded against THAT account and never blocks the
others or corrupts the cache. The write is MERGE-preserving: an account that
fails this round keeps whatever entry it had from a prior successful round
(a transient blip must not wipe good data the picker relies on). The caller
(the CLI) exits non-zero only when EVERY attempted account failed.

Zero accounts is NOT a refresh
------------------------------
The merge above preserves entries for accounts that FAIL — a set that is
EMPTY when the store holds no accounts at all, so it protects nothing in
that case. An empty store used to still WRITE: it restamped ``written_at``
on a cache nothing had re-measured, and on a host with no cache it CREATED
``{"accounts": {}}`` — a file that makes :func:`_account.quota_cache.
quota_cache_present` report ``True``. That flip is not cosmetic: the boot
picker's ``require_quota_evidence`` gate is keyed off exactly that predicate,
so an empty cache converts a boot that would have degraded to freshness-only
into a hard refusal. ``accounts_found == 0`` therefore writes NOTHING and
reports ``written=False`` / ``reason="no-accounts"``, which the CLI maps to
its own exit code.

Token material never leaves :mod:`_account.claude_usage`; this module handles
only percentages + TTL hours.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .claude_usage import _read_tokens_at, fetch_usage_for_credentials
from .quota_cache import read_quota_entry, write_quota_cache

# Type of the injectable usage fetcher — takes a per-account credentials path,
# returns the ``fetch_usage_for_credentials`` result dict (``used_pct_5h`` /
# ``used_pct_7d`` / ``error`` / ...). Injected in tests to avoid the network.
UsageFetcher = Callable[[Path], dict[str, Any]]

_CREDENTIALS_FILENAME = ".credentials.json"


def _short_of(account_name: str) -> str:
    """First dash-segment of the account slug — the reader's match key.

    Mirrors :func:`_account.quota_cache.read_quota_entry`'s rule
    (``ywatanabe-scitex-ai`` → ``ywatanabe``) so a written entry's ``short``
    is exactly what the reader looks up ``$CLAUDE_AGENT_ACCOUNT`` against.
    """
    return account_name.split("-", 1)[0]


def _ttl_hours(credentials_path: Path, now: float) -> float | None:
    """Hours until the snapshot's OAuth access token expires, or ``None``.

    Reads ONLY the non-secret ``expiresAt`` (ms epoch) via the shared
    token reader; the token values themselves are discarded. ``None`` when
    the snapshot lacks a parseable expiry.
    """
    _, _, _, expires_at_ms = _read_tokens_at(credentials_path)
    if not isinstance(expires_at_ms, int):
        return None
    return round((expires_at_ms / 1000.0 - now) / 3600.0, 2)


def refresh_quota_cache(
    *,
    home: Path | None = None,
    store_dir: Path | None = None,
    cache_path: Path | str | None = None,
    usage_fetcher: UsageFetcher | None = None,
    now: float | None = None,
    merge: bool = True,
) -> dict[str, Any]:
    """Fetch usage for every stored account and (re)write ``quota-cache.json``.

    Args:
        home: Home dir override (tests). Defaults to ``Path.home()``.
        store_dir: Account-store dir override (tests). ``None`` walks the
            SciTeX local-state cascade like the picker does.
        cache_path: Where to write. ``None`` → ``$SAC_QUOTA_CACHE_PATH`` →
            ``~/.scitex/quota-cache.json`` (see
            :func:`_account.quota_cache._resolve_write_cache_path`).
        usage_fetcher: Injection seam for the usage-fetch boundary. Defaults
            to :func:`_account.claude_usage.fetch_usage_for_credentials`.
        now: Epoch-seconds override for the TTL math (tests).
        merge: When ``True`` (default) preserve prior entries for accounts
            that FAIL this round, so a transient error never drops good data.

    Returns:
        ``{"cache_path": str, "written": bool, "reason": str | None,
           "accounts_found": int, "ok": int, "failed": int,
           "results": [{"name", "short", "h5", "d7", "ttl_h",
                        "reset_at_5h", "reset_at_7d", "error"}]}``.
        ``error`` is ``None`` on success. The two ``reset_at_*`` stamps
        are ISO-8601 strings (or ``None`` when upstream omits them) and
        are what lets the picker tell expiring quota from a reserve —
        see :func:`_creds._quota_rank.is_expiring_7d`.

        ``accounts_found == 0`` is a THIRD outcome, neither refreshed nor
        failed: nothing is written, ``written`` is ``False`` and ``reason``
        is ``"no-accounts"``. Every other run writes and reports
        ``reason=None``. Never raises.
    """
    from .._state.account_store import list_accounts

    _home = home if home is not None else Path.home()
    _now = now if now is not None else time.time()
    _fetch = usage_fetcher if usage_fetcher is not None else fetch_usage_for_credentials

    store = _store_dir(store_dir, _home)

    # Seed with prior entries so a per-account failure preserves good data.
    accounts_out: dict[str, Any] = {}
    if merge:
        accounts_out = _load_existing_accounts(cache_path, _home)

    results: list[dict[str, Any]] = []
    ok = 0
    failed = 0
    names = [
        meta.get("name") for meta in list_accounts(store_dir=store_dir, home=_home)
    ]
    names = [n for n in names if isinstance(n, str) and n]

    if not names:
        return _no_accounts_result(cache_path, _home)

    for name in names:
        row = _refresh_one(name, store, _fetch, _now)
        results.append(row)
        if row["error"] is None:
            accounts_out[name] = {
                "short": row["short"],
                "h5": row["h5"],
                "d7": row["d7"],
                "ttl_h": row["ttl_h"],
                # WHEN each window resets — not just how full it is. The
                # picker cannot tell "90%, resets in 6 minutes" (spend it,
                # it is about to be deleted) from "90%, resets in 6 days"
                # (a reserve, leave it) without this. The usage API has
                # always returned it; we simply dropped it here, so the
                # ranker was structurally unable to see it and the fleet
                # binned the tail of every 7d window. Optional (None when
                # upstream omits it) — every reader degrades to the
                # reset-unaware behaviour rather than guess.
                "reset_at_5h": row["reset_at_5h"],
                "reset_at_7d": row["reset_at_7d"],
            }
            ok += 1
        else:
            failed += 1

    written_path = write_quota_cache(
        accounts_out, cache_path=cache_path, home=_home, written_at=_now
    )
    return {
        "cache_path": str(written_path),
        "written": True,
        "reason": None,
        "accounts_found": len(names),
        "ok": ok,
        "failed": failed,
        "results": results,
    }


#: ``reason`` value for the zero-accounts outcome — the token the CLI maps
#: to its own exit code and any caller can branch on without parsing prose.
REASON_NO_ACCOUNTS = "no-accounts"


def _no_accounts_result(
    cache_path: Path | str | None,
    home: Path,
) -> dict[str, Any]:
    """The zero-accounts outcome: report the path, write nothing to it.

    Deliberately does not call :func:`write_quota_cache`. There is nothing
    to merge and nothing to record, so the only two things a write could do
    are both damage: restamp ``written_at`` on data no one re-measured, or
    materialise an empty cache whose mere EXISTENCE arms the boot picker's
    ``require_quota_evidence`` gate (see :func:`_account.quota_cache.
    quota_cache_present`).
    """
    from .quota_cache import _resolve_write_cache_path

    return {
        "cache_path": str(_resolve_write_cache_path(cache_path, home)),
        "written": False,
        "reason": REASON_NO_ACCOUNTS,
        "accounts_found": 0,
        "ok": 0,
        "failed": 0,
        "results": [],
    }


def _store_dir(store_dir: Path | None, home: Path) -> Path:
    """Resolve the account-store directory (shared with the picker)."""
    from .._state.account_store import _store_path

    return _store_path(store_dir, home)


def _load_existing_accounts(
    cache_path: Path | str | None,
    home: Path,
) -> dict[str, Any]:
    """Best-effort read of the current cache's ``accounts`` map for merging."""
    import json

    from .quota_cache import _resolve_write_cache_path

    path = _resolve_write_cache_path(cache_path, home)
    # stx-allow: fallback (reason: a missing/corrupt prior cache is the normal
    # cold-start case; degrade to an empty seed rather than fail the refresh.)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (
        Exception
    ):  # stx-allow: fallback (reason: catch-all safety net — see inline comment)
        return {}
    accounts = parsed.get("accounts") if isinstance(parsed, dict) else None
    return dict(accounts) if isinstance(accounts, dict) else {}


def _refresh_one(
    name: str,
    store: Path,
    fetch: UsageFetcher,
    now: float,
) -> dict[str, Any]:
    """Fetch one account's usage + TTL into a result row. Never raises."""
    row: dict[str, Any] = {
        "name": name,
        "short": _short_of(name),
        "h5": None,
        "d7": None,
        "ttl_h": None,
        "reset_at_5h": None,
        "reset_at_7d": None,
        "error": None,
    }
    creds_path = store / name / _CREDENTIALS_FILENAME
    if not creds_path.is_file():
        row["error"] = f"no credentials snapshot at {creds_path}"
        return row

    # stx-allow: fallback (reason: the injected fetcher is documented
    # never-raise, but defence-in-depth so one bad account never aborts the
    # whole refresh loop or corrupts the cache.)
    try:
        usage = fetch(creds_path)
    except (
        Exception
    ) as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment)
        row["error"] = f"usage fetch raised: {exc}"
        return row

    if not isinstance(usage, dict):
        row["error"] = "usage fetch returned no data"
        return row
    if usage.get("error"):
        row["error"] = str(usage["error"])
        return row

    h5 = usage.get("used_pct_5h")
    d7 = usage.get("used_pct_7d")
    if not _is_pct(h5) or not _is_pct(d7):
        row["error"] = "usage response missing 5h/7d utilisation"
        return row

    ttl_h = _ttl_hours(creds_path, now)
    if ttl_h is None:
        row["error"] = "no parseable token expiry in snapshot"
        return row

    row["h5"] = float(h5)
    row["d7"] = float(d7)
    row["ttl_h"] = ttl_h
    # Reset stamps are OPTIONAL, unlike h5/d7/ttl_h above: a response that
    # omits them still yields a usable entry (the picker just degrades to
    # its reset-unaware ranking for that account). Making them required
    # would let a single upstream field change blank the whole cache.
    row["reset_at_5h"] = _iso_or_none(usage.get("reset_at_5h"))
    row["reset_at_7d"] = _iso_or_none(usage.get("reset_at_7d"))
    return row


def _iso_or_none(value: Any) -> str | None:
    """Pass through a non-empty ISO-8601 string; anything else → ``None``."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _is_pct(value: Any) -> bool:
    """A real number (bool rejected — it's an int in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = [
    "REASON_NO_ACCOUNTS",
    "refresh_quota_cache",
    "read_quota_entry",  # re-export for callers verifying round-trips
    "UsageFetcher",
]
