"""Quota-conditional ranking + fleet load-balancing for the account picker.

Split out of :mod:`._pick_healthy` (512-line module limit). This module
owns the QUOTA side of the pick — reading per-account 5h/7d utilisation
from the cached ``quota-cache.json`` and turning it into a *conditional*
ranking — while ``_pick_healthy`` keeps the freshness gate and the
public :func:`~._pick_healthy.pick_healthy_account` entry point.

Why conditional (2026-07 incident, ``sac-restart --all-running``)
-----------------------------------------------------------------
The Phase-1 pick ordered fresh accounts by a single scalar — 7d % —
and kept the ``preferred`` entry whenever it was below the 7d near-cap.
Two failure modes followed on the same restart:

* an account at **100% of its 5h window** (cannot run *now*, resets in
  hours) was selected because its 7d % looked fine (60% < 90%);
* every agent in the fleet computed the same deterministic answer, so
  a bulk restart stacked ALL agents onto that one account while two
  accounts with 0% 5h sat idle.

The two quota axes mean different things and need different rules:

* **5h % ("can it run NOW")** — an account at/above
  :data:`BLOCKED_5H_PCT` is *blocked-now*: it 429s immediately and
  recovers within ≤5h. Never pick it while any fresh alternative is
  unblocked; never keep a blocked-now ``preferred``.
* **7d % ("sustained budget")** — the existing
  :data:`NEAR_CAP_7D_PCT` avoidance, plus a *weight* for spreading:
  more weekly headroom → proportionally more of the fleet.

Ranking (lexicographic tiers, then in-tier choice)
--------------------------------------------------
Fresh candidates are partitioned by ``(blocked_now, near_capped_7d,
d7_unknown)`` and only the best non-empty tier competes. Unknown values
degrade per account (an absent/stale cache never blocks a boot): an
unknown 5h % reads as "not blocked", an unknown 7d % sorts behind every
known one (known headroom is never displaced by a guess).

Within the winning tier:

* with a ``spread_key`` (the agent name) — **weighted rendezvous
  hashing**: score every account by a stable per-(agent, account) hash
  scaled by its 7d headroom and take the max. Deterministic for a given
  agent (same pick on every restart while quota tiers are unchanged →
  no churn), but *different* agents land on *different* accounts in
  proportion to headroom — the load-balancing the incident demanded.
  ``hashlib`` (not ``hash()``) so the mapping survives interpreter
  restarts and PYTHONHASHSEED.
* without a ``spread_key`` — the legacy deterministic order: lowest
  7d %, then lowest 5h %, then candidate order.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Mapping

from .._account.quota_cache import read_quota_entry

# 7d-utilisation threshold above which an account is "near-capped —
# avoid unless no better fresh alternative". See _pick_healthy's module
# docstring for the incident history. A preference, never a hard gate.
NEAR_CAP_7D_PCT = 90.0

# 5h-utilisation threshold at/above which an account is "blocked-now":
# it cannot serve requests immediately (429 until the 5h window
# resets). 95 rather than 100 because the cache is cron-refreshed and
# lags live usage by minutes — an account reading 95%+ is realistically
# at the wall by boot time. Also a preference (an all-blocked fleet
# still boots on the least-bad account), never a hard gate.
BLOCKED_5H_PCT = 95.0


def _coerce_pct(value: object) -> float | None:
    """Return *value* as a float utilisation %, or ``None`` if not numeric.

    ``bool`` is explicitly rejected (a ``bool`` is an ``int`` in Python)
    so ``True`` never surfaces as ``1.0%`` — mirrors
    :func:`_account.quota_cache._is_number`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _account_usage_pct(
    name: str,
    field: str,
    *,
    override: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> float | None:
    """Return one account's cached utilisation % for *field*, or ``None``.

    ``field`` is the quota-cache entry key (``"h5"`` / ``"d7"``). Reads
    via :func:`_account.quota_cache.read_quota_entry` (never raises — a
    missing / stale / unreadable cache, or no matching entry, all
    collapse to ``None``). ``None`` is the caller's signal to degrade
    for *that* account.

    ``override`` is the test-injection seam: a name → pct mapping
    consulted INSTEAD of the on-disk cache (a missing key reads as
    ``None``). Mirrors the ``store_dir`` / ``home`` / ``now`` idiom in
    ``_pick_healthy`` so tests need no real ``quota-cache.json``.
    """
    if override is not None:
        return _coerce_pct(override.get(name))
    entry = read_quota_entry(account=name, cache_path=quota_cache_path)
    if entry is None:
        return None
    return _coerce_pct(entry.get(field))


def account_5h_usage(
    name: str,
    *,
    usage_5h: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> float | None:
    """One account's cached 5h utilisation % (``h5``), or ``None``."""
    return _account_usage_pct(
        name, "h5", override=usage_5h, quota_cache_path=quota_cache_path
    )


def account_7d_usage(
    name: str,
    *,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> float | None:
    """One account's cached 7d utilisation % (``d7``), or ``None``."""
    return _account_usage_pct(
        name, "d7", override=usage_7d, quota_cache_path=quota_cache_path
    )


def _hrw_score(spread_key: str, name: str, weight: float) -> float:
    """Weighted rendezvous (HRW) score for one (agent, account) pair.

    Standard weighted-HRW: map the pair to a uniform ``u ∈ (0, 1)`` via
    a stable hash, score ``-weight / ln(u)``; the caller takes the max.
    Properties this buys: per-agent deterministic (no churn across
    restarts), fleet-wide spread proportional to ``weight``, and
    minimal reshuffling when an account enters/leaves the eligible tier
    (only that account's agents move).
    """
    digest = hashlib.sha256(f"{spread_key}\x00{name}".encode("utf-8")).digest()
    u = (int.from_bytes(digest[:8], "big") + 0.5) / 2.0**64
    return -weight / math.log(u)


def pick_ranked(
    names: list[str],
    usage_5h: Mapping[str, float | None],
    usage_7d: Mapping[str, float | None],
    *,
    spread_key: str | None = None,
    near_cap_pct: float = NEAR_CAP_7D_PCT,
    blocked_5h_pct: float = BLOCKED_5H_PCT,
) -> str:
    """Return the best account among *names* (all token-fresh, non-empty).

    ``usage_5h`` / ``usage_7d`` map each name to its cached utilisation
    % or ``None`` (unknown). See the module docstring for the tier
    rules; this never raises on quota state (fail-loud for "nothing
    fresh" lives in the caller).
    """

    def tier(name: str) -> tuple[bool, bool, bool]:
        h5 = usage_5h.get(name)
        d7 = usage_7d.get(name)
        blocked_now = h5 is not None and h5 >= blocked_5h_pct
        near_capped = d7 is not None and d7 >= near_cap_pct
        return (blocked_now, near_capped, d7 is None)

    best = min(tier(n) for n in names)
    group = [n for n in names if tier(n) == best]
    if len(group) == 1:
        return group[0]

    if spread_key:

        def weight(name: str) -> float:
            d7 = usage_7d.get(name)
            if d7 is None:
                return 1.0
            return max(1.0, 100.0 - d7)

        return max(group, key=lambda n: _hrw_score(spread_key, n, weight(n)))

    # Legacy deterministic order (no spread requested): most 7d headroom,
    # then most 5h headroom, then candidate order. Unknowns sort last via
    # +inf so a known value always wins inside the tie-break.
    def sort_key(item: tuple[int, str]) -> tuple[float, float, int]:
        idx, name = item
        d7 = usage_7d.get(name)
        h5 = usage_5h.get(name)
        return (
            d7 if d7 is not None else math.inf,
            h5 if h5 is not None else math.inf,
            idx,
        )

    return min(enumerate(group), key=lambda it: sort_key(it))[1]


__all__ = [
    "BLOCKED_5H_PCT",
    "NEAR_CAP_7D_PCT",
    "account_5h_usage",
    "account_7d_usage",
    "pick_ranked",
]
