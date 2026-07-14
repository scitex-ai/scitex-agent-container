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

Expiring capacity — the third axis (2026-07-14)
-----------------------------------------------
A utilisation % alone is NOT a decision. The ranker scored "90%, resets
in 6 minutes" and "90%, resets in 6 days" identically and avoided both,
but they are opposites: the first is quota that is about to be DELETED,
the second is a genuine reserve. So every cycle the fleet binned the
last ~10% of a 7-day window unused (operator: 「毎回 90%で10%捨ててる」).

The window's reset time — already fetched from the usage API and shown
by ``sac accounts list`` — was simply never persisted into the cache the
picker reads. It is now (``reset_at_7d``; see
:func:`_account.quota_cache_refresh.refresh_quota_cache`), and
:func:`is_expiring_7d` turns it into the missing distinction:
near-capped-but-expiring stops being *avoided* (it is not scarce) and
starts being *preferred* (spend what vanishes before what persists).
The preference is expressed as a spread WEIGHT, not a hard tier, so it
cannot re-create the stacking incident below.

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
import time
from datetime import datetime, timezone
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

# --- Expiring-capacity ("use it or lose it") ------------------------------
#
# Seconds-to-7d-reset at/below which a near-capped account's REMAINING
# quota is reclassified from "scarce reserve" to "about to be deleted".
#
# Why 2h (and not "any account that will reset eventually"): the ONLY
# reason to avoid a near-capped account is that an agent parked there
# would burn the remainder and then be stuck until the window resets.
# The cost of being wrong is therefore bounded by TIME-TO-RESET — so
# that is exactly what we bound. 2h keeps the worst-case "stuck at the
# cap" exposure short while still giving the fleet a real drain window
# before every reset (each account passes through it once per cycle).
# Widen it to drain more aggressively; the 429 exposure scales with it.
EXPIRING_7D_HORIZON_S = 2.0 * 3600.0

# An account must retain at least this much 7d headroom to count as
# "expiring capacity worth spending". Without this floor a FULLY
# exhausted account (d7 == 100) resetting in 5 minutes would look like
# free capacity and we would route work onto an account that 429s on
# its very first request — burning the "expiring" preference for
# nothing. There is no capacity to reclaim below this line.
EXPIRING_MIN_HEADROOM_PCT = 2.0

# Multiplier applied to an expiring account's rendezvous-hash WEIGHT.
#
# This is the load-balancing-safe expression of "prefer expiring
# capacity": a boosted weight makes the fleet drain the vanishing quota
# FASTER without making it the single winner for every agent. Promoting
# expiring accounts to their own hard TIER instead would put every
# booting agent on one account holding ~10% of a week — precisely the
# stacking incident this module was written to prevent (see the module
# docstring). Weight ∝ headroom keeps each account's share bounded by
# the capacity it actually has.
EXPIRING_WEIGHT_BOOST = 4.0


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


def _coerce_epoch(value: object) -> float | None:
    """Return *value* as epoch seconds, or ``None`` if unparseable.

    Tolerant on purpose — the quota cache stores the reset stamp as the
    upstream ISO-8601 string (``reset_at_7d``), but tests and callers
    find raw epoch floats easier to reason about, so both are accepted.
    A naive (tz-less) ISO stamp is read as UTC, matching
    :func:`cli_pkg._account_list_format._coerce_dt`.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    # stx-allow: fallback (reason: a malformed cache timestamp must degrade to
    # "reset unknown" — i.e. the pre-existing behaviour — never crash a boot.)
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def account_7d_reset_at(
    name: str,
    *,
    reset_7d: Mapping[str, object] | None = None,
    quota_cache_path: Path | str | None = None,
) -> float | None:
    """One account's 7d-window reset time as epoch seconds, or ``None``.

    Reads the ``reset_at_7d`` field the quota-cache populator persists
    (:func:`_account.quota_cache_refresh.refresh_quota_cache`). ``None``
    for a cache written BEFORE that field existed — which is exactly why
    every consumer below must degrade to the reset-unaware behaviour
    rather than assume "unknown reset" means "resets now".
    """
    if reset_7d is not None:
        return _coerce_epoch(reset_7d.get(name))
    entry = read_quota_entry(account=name, cache_path=quota_cache_path)
    if entry is None:
        return None
    return _coerce_epoch(entry.get("reset_at_7d"))


def is_expiring_7d(
    d7: float | None,
    reset_at: float | None,
    now: float,
    *,
    horizon_s: float = EXPIRING_7D_HORIZON_S,
    min_headroom_pct: float = EXPIRING_MIN_HEADROOM_PCT,
) -> bool:
    """Is this account's remaining 7d quota USE-IT-OR-LOSE-IT right now?

    The distinction the ranker was missing (operator, 2026-07-14: 「毎回
    90%で10%捨ててる」). ``d7 = 90%`` means two OPPOSITE things depending
    on a number the ranker never looked at:

    * **90%, resets in 6 days** — a genuine reserve. Spend it and the
      fleet is stuck without weekly budget. Avoiding it is CORRECT.
    * **90%, resets in 6 minutes** — the remaining 10% is about to be
      DELETED. Avoiding it burns the capacity for nothing.

    Only the second is "expiring". Returns ``True`` only when all hold:

    * the 7d utilisation is KNOWN (an unknown % cannot be reasoned about);
    * the reset stamp is KNOWN and lies within ``horizon_s`` — an unknown
      or already-past stamp returns ``False``, i.e. the pre-existing
      near-cap avoidance, so a stale/old cache is never *more* risky than
      today's behaviour;
    * at least ``min_headroom_pct`` of the window remains — there is no
      capacity to reclaim from an account that is already at the wall.

    Deliberately says nothing about the 5h window: that is the IMMEDIATE
    throttle and stays a separate, supreme tier in :func:`pick_ranked`.
    An expiring account that is 5h-blocked still loses — it cannot serve
    a request *now* no matter how soon its weekly budget refreshes.
    """
    if d7 is None or reset_at is None:
        return False
    secs_to_reset = reset_at - now
    if secs_to_reset < 0.0 or secs_to_reset > horizon_s:
        return False
    return d7 <= 100.0 - min_headroom_pct


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
    reset_7d: Mapping[str, object] | None = None,
    now: float | None = None,
    spread_key: str | None = None,
    near_cap_pct: float = NEAR_CAP_7D_PCT,
    blocked_5h_pct: float = BLOCKED_5H_PCT,
    expiring_horizon_s: float = EXPIRING_7D_HORIZON_S,
) -> str:
    """Return the best account among *names* (all token-fresh, non-empty).

    ``usage_5h`` / ``usage_7d`` map each name to its cached utilisation
    % or ``None`` (unknown). ``reset_7d`` maps each name to its 7d-window
    reset stamp (ISO string or epoch seconds; ``None``/absent = unknown).
    See the module docstring for the tier rules; this never raises on
    quota state (fail-loud for "nothing fresh" lives in the caller).

    Expiring capacity (see :func:`is_expiring_7d`) enters the ranking in
    exactly two places, and NOWHERE else:

    1. it SUPPRESSES the ``near_capped`` demotion — quota that is about
       to be deleted is expiring, not scarce, so the account competes on
       equal footing instead of being avoided;
    2. it BOOSTS the account's spread weight (and sorts first in the
       no-spread order) — we spend what is about to vanish before we
       spend a reserve that persists.

    ``blocked_now`` (the 5h window) is untouched and still dominates
    both: an account that cannot serve a request *now* is never picked
    over one that can, however soon its weekly budget refreshes.

    With no ``reset_7d`` data (a cache predating the field) every
    account reads "reset unknown" → ``is_expiring_7d`` is ``False``
    everywhere → this function behaves EXACTLY as it did before.
    """
    _now = now if now is not None else time.time()
    _resets: Mapping[str, object] = reset_7d if reset_7d is not None else {}

    def expiring(name: str) -> bool:
        return is_expiring_7d(
            usage_7d.get(name),
            _coerce_epoch(_resets.get(name)),
            _now,
            horizon_s=expiring_horizon_s,
        )

    def tier(name: str) -> tuple[bool, bool, bool]:
        h5 = usage_5h.get(name)
        d7 = usage_7d.get(name)
        blocked_now = h5 is not None and h5 >= blocked_5h_pct
        # RULE 1 — an expiring window is NOT a scarce one. Its remaining
        # quota evaporates at the reset whether we spend it or not.
        near_capped = d7 is not None and d7 >= near_cap_pct and not expiring(name)
        return (blocked_now, near_capped, d7 is None)

    best = min(tier(n) for n in names)
    group = [n for n in names if tier(n) == best]
    if len(group) == 1:
        return group[0]

    if spread_key:

        def weight(name: str) -> float:
            d7 = usage_7d.get(name)
            base = 1.0 if d7 is None else max(1.0, 100.0 - d7)
            # RULE 2 (fleet path) — drain vanishing capacity faster, but
            # as a WEIGHT, never a hard tier: the account still takes a
            # bounded share proportional to what it actually holds, so a
            # bulk restart cannot stack the whole fleet onto a window
            # with 10% left.
            return base * EXPIRING_WEIGHT_BOOST if expiring(name) else base

        return max(group, key=lambda n: _hrw_score(spread_key, n, weight(n)))

    # Legacy deterministic order (no spread requested): expiring capacity
    # first (RULE 2 — spend what vanishes before what persists), then most
    # 7d headroom, then most 5h headroom, then candidate order. Unknowns
    # sort last via +inf so a known value always wins inside the tie-break.
    def sort_key(item: tuple[int, str]) -> tuple[int, float, float, int]:
        idx, name = item
        d7 = usage_7d.get(name)
        h5 = usage_5h.get(name)
        return (
            0 if expiring(name) else 1,
            d7 if d7 is not None else math.inf,
            h5 if h5 is not None else math.inf,
            idx,
        )

    return min(enumerate(group), key=lambda it: sort_key(it))[1]


__all__ = [
    "BLOCKED_5H_PCT",
    "EXPIRING_7D_HORIZON_S",
    "EXPIRING_MIN_HEADROOM_PCT",
    "EXPIRING_WEIGHT_BOOST",
    "NEAR_CAP_7D_PCT",
    "account_5h_usage",
    "account_7d_reset_at",
    "account_7d_usage",
    "is_expiring_7d",
    "pick_ranked",
]
