"""Pick a healthy stored account at agent-start.

CREDS-PHASE1: an agent pins ``spec.claude.account`` to one of the
operator's saved accounts. When that account's stored credential
snapshot is EXPIRED or ABSENT, today the start fails (see
:class:`scitex_agent_container.runtimes._apptainer_creds.PinnedAccountError`)
and the operator has to manually ``claude /login`` + ``sac accounts
sync-live`` before the agent can run again — even when a *different*
saved account has a perfectly fresh credential.

This picker is the boot-time first pass that rotates around that
manual step: keep the pinned account when it's healthy; otherwise
hand the start a different account whose snapshot IS healthy; raise
loudly when NOTHING is healthy (the genuine "all three accounts
capped/expired" case the operator must fix).

Scope (Phase 1, deliberately small)
-----------------------------------
* Health is *snapshot freshness*: a non-expired ``claudeAiOauth.
  expiresAt`` (the same field :mod:`_account.creds_sync` already
  reads) → ``VALID``. EXPIRED / ABSENT → unhealthy.
* Account-pool Phase 2 — *quota-conditional* pick with fleet
  load-balancing: per-account 5h and 7d utilisation are read from the
  cached ``quota-cache.json`` the apptainer runtime already binds (see
  :mod:`_account.quota_cache`) — a cheap, *cache-only* read, NO live
  Claude API call, so it never burns account quota at boot. The two
  axes carry different rules (see :mod:`._quota_rank`): an account at
  ≥ ~95% of its **5h** window is *blocked-now* (429s immediately) and
  is avoided while any fresh alternative is unblocked; an account at
  ≥ ~90% **7d** is *near-capped* (the existing avoidance) — UNLESS its
  7d window resets within the hour(s), in which case the remainder is
  EXPIRING, not scarce, and we spend it rather than let it be deleted
  (:func:`._quota_rank.is_expiring_7d`). The
  ``preferred`` account is kept only when it is fresh AND not
  blocked-now AND not near-capped, to minimise churn. Otherwise the
  best tier of fresh candidates competes: with a ``spread_key`` (the
  agent name) the winner is chosen by 7d-headroom-weighted rendezvous
  hashing so a bulk fleet restart SPREADS agents across healthy
  accounts instead of stacking them all onto one; without a spread key
  the lowest-7d% candidate wins (legacy order). Both thresholds are
  preferences, not hard gates — a boot is only ever blocked on
  freshness. Cap-induced 429s still surface from claude in-turn
  (runtime failover is a later phase).
* GRACEFUL DEGRADATION: the quota cache is best-effort. When it is
  absent / stale / unreadable for an account (or entirely — e.g. a
  host whose cron has not populated it yet), that account degrades to
  the freshness-only behavior — never a crash, never a blocked boot.
* Read-only: this module NEVER writes a snapshot. The existing
  per-agent writable boot-copy in
  :func:`runtimes._apptainer_creds.resolve_cred_file` and its
  in-container ~1h token refresh are untouched.
* Pure stdlib; no network call.

Fail-loud contract
------------------
``pick_healthy_account`` raises :class:`NoHealthyAccountError` when
nothing is healthy. The message names every candidate's state so the
operator immediately sees which accounts to refresh. No silent
fallback to a stale snapshot — running a pinned agent on a known-
expired token is exactly what the previous fail-loud guard was added
to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .._account.creds_sync import (
    _oauth_expiry_to_seconds,
    _read_oauth_expiry_raw,
)
from .._state.account_store import _store_path
from ._quota_rank import (
    BLOCKED_5H_PCT,
    EXPIRING_7D_HORIZON_S,
    NEAR_CAP_7D_PCT,
    account_5h_usage,
    account_7d_reset_at,
    account_7d_usage,
    is_expiring_7d,
    pick_ranked,
)

# Health states for one account's snapshot. Mirrors
# :class:`_account.creds_sync.Freshness` but anchored on a *name* so the
# picker can return a structured "why I rotated" record to its caller.
_VALID = "VALID"
_EXPIRED = "EXPIRED"
_ABSENT = "ABSENT"

# Thresholds live in ._quota_rank (shared with the ranking); the local
# aliases keep this module's public parameter defaults stable.
_NEAR_CAP_PCT = NEAR_CAP_7D_PCT
_BLOCKED_5H_PCT = BLOCKED_5H_PCT


class NoHealthyAccountError(RuntimeError):
    """Raised when no stored account currently has a usable snapshot.

    The message names every probed account and its state so the
    operator sees the full picture in one error line — they should
    ``claude /login`` to one of them and ``sac accounts sync-live``,
    then restart the agent. Surfaced through ``sac agents start``.
    """


@dataclass(frozen=True)
class AccountHealth:
    """Health of one stored account's credential snapshot.

    Attributes
    ----------
    name
        The stored-account name (a slug — see
        :func:`_account.creds_sync.slugify_email`).
    state
        ``"VALID"`` (non-expired snapshot present), ``"EXPIRED"`` (a
        snapshot exists but its ``expiresAt`` is in the past), or
        ``"ABSENT"`` (no snapshot file on disk, or unparseable).
    hours_remaining
        Signed hours to expiry (positive = remaining, negative =
        past) for VALID/EXPIRED; ``None`` for ABSENT.
    snapshot_path
        The EXACT file this probe read (or looked for). INCIDENT
        2026-07-10: the "EXPIRED (-5.8h)" boot error hid which file it
        had evaluated, so a snapshot repaired minutes later looked like
        a false read and cost the investigation hours. Every health
        record now carries its evidence.
    expires_at_raw
        The literal ``claudeAiOauth.expiresAt`` value found in the file
        (claude-code writes unix milliseconds); ``None`` for ABSENT.
    """

    name: str
    state: str
    hours_remaining: float | None
    snapshot_path: str | None = None
    expires_at_raw: float | None = None

    @property
    def is_healthy(self) -> bool:
        return self.state == _VALID


def account_health(
    name: str,
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> AccountHealth:
    """Return the health of one stored account's snapshot.

    Never raises. A missing / unparseable snapshot reads as
    :class:`AccountHealth` ``state="ABSENT"``.

    This is functionally equivalent to
    :func:`_account.creds_sync.account_freshness`, re-exposed under a
    name-anchored shape so :func:`pick_healthy_account` can build the
    diagnostic error message without losing the account name.
    """
    _home = home if home is not None else Path.home()
    now_ts = now if now is not None else time.time()

    store = _store_path(store_dir, _home)
    snapshot = store / name / ".credentials.json"
    # ONE read supplies BOTH the raw evidence value and the normalised
    # expiry, so the record can never quote a different file state than
    # the one it judged (a mid-probe rewrite yields a coherent record).
    raw = _read_oauth_expiry_raw(snapshot) if snapshot.is_file() else None
    if raw is None:
        return AccountHealth(
            name=name,
            state=_ABSENT,
            hours_remaining=None,
            snapshot_path=str(snapshot),
            expires_at_raw=None,
        )
    expiry = _oauth_expiry_to_seconds(raw)
    hours = (expiry - now_ts) / 3600.0
    state = _VALID if expiry > now_ts else _EXPIRED
    return AccountHealth(
        name=name,
        state=state,
        hours_remaining=hours,
        snapshot_path=str(snapshot),
        expires_at_raw=raw,
    )


def _discover_candidates(
    store_dir: Path | None,
    home: Path,
) -> list[str]:
    """Return every stored-account name on disk (sorted, deterministic).

    Best-effort: a missing / unreadable store reads as no candidates
    (the caller turns that into :class:`NoHealthyAccountError`). We
    skip the store-internal ``_rotations/`` housekeeping dir so it
    can never get picked.
    """
    store = _store_path(store_dir, home)
    if not store.is_dir():
        return []
    out: list[str] = []
    # stx-allow: fallback (reason: store iteration is best-effort
    # discovery; an unreadable dir / partially-created entry must
    # degrade to "no candidates" so the caller's fail-loud takes over,
    # never crash with an OSError mid-resolution.)
    try:
        for child in sorted(store.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("_"):
                continue
            out.append(child.name)
    except OSError:
        return []
    return out


def _iso_utc(seconds: float) -> str:
    """Render a unix-seconds timestamp as a compact ISO-8601 UTC string."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _format_states(healths: list[AccountHealth]) -> str:
    """Render the operator-facing per-account evidence list for the error.

    Self-diagnosing (INCIDENT 2026-07-10): each entry names the file
    that was read, the RAW ``expiresAt`` value found in it, and its UTC
    rendering — so a "this account was fine minutes later!" report can
    be adjudicated from the error line alone (the snapshot may simply
    have been rewritten between the probe and the re-check).
    """
    parts: list[str] = []
    for h in healths:
        if h.hours_remaining is None or h.expires_at_raw is None:
            parts.append(
                f"{h.name}={h.state} (no numeric claudeAiOauth.expiresAt; "
                f"file={h.snapshot_path})"
            )
        else:
            expiry_s = _oauth_expiry_to_seconds(h.expires_at_raw)
            parts.append(
                f"{h.name}={h.state} ({h.hours_remaining:+.1f}h; "
                f"expiresAt={h.expires_at_raw:.0f} = {_iso_utc(expiry_s)}; "
                f"file={h.snapshot_path})"
            )
    return ", ".join(parts) if parts else "(no candidates)"


def pick_healthy_account(
    preferred: str | None,
    *,
    candidates: list[str] | None = None,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
    usage_5h: Mapping[str, float] | None = None,
    usage_7d: Mapping[str, float] | None = None,
    reset_7d: Mapping[str, object] | None = None,
    quota_cache_path: Path | str | None = None,
    near_cap_pct: float = _NEAR_CAP_PCT,
    blocked_5h_pct: float = _BLOCKED_5H_PCT,
    expiring_horizon_s: float = EXPIRING_7D_HORIZON_S,
    spread_key: str | None = None,
) -> str:
    """Return the stored-account name an agent should run on right now.

    Preference order (account-pool Phase 2 — quota-conditional)
    -----------------------------------------------------------
    1. ``preferred`` (typically ``spec.claude.account``) when its
       snapshot is :attr:`AccountHealth.is_healthy` AND it is NOT
       blocked-now (cached 5h utilisation below ``blocked_5h_pct``, or
       unknown) AND it is NOT near-capped (cached 7d utilisation below
       ``near_cap_pct``, or unknown). Unknown quota degrades to
       freshness-only and keeps the preferred — this minimises churn;
       we only rotate off ``preferred`` on *known* bad quota.
    2. Otherwise, the best tier of token-fresh candidates competes —
       see :func:`._quota_rank.pick_ranked`: unblocked-now beats
       5h-blocked, 7d-headroom beats near-capped, known 7d beats
       unknown. Within the winning tier, a ``spread_key`` selects by
       7d-headroom-weighted rendezvous hashing (per-agent deterministic
       fleet spread); without one the lowest 7d % wins (ties: lowest
       5h %, then candidate order).

    Quota is a *preference*, not a hard gate: when every fresh
    candidate is blocked or near-capped, the least-bad fresh one is
    still returned (a boot is never blocked on quota — only on there
    being NOTHING token-fresh, which stays fail-loud).

    Parameters
    ----------
    preferred
        The agent's pinned account (``spec.claude.account``). ``None``
        / empty means "no preference" — the picker hands back the
        ranked winner instead of raising.
    candidates
        Optional explicit candidate shortlist. ``None`` (default) walks
        every stored account directory on disk. An EMPTY list is
        respected (no auto-discovery fallback) so callers can pin the
        candidate universe in tests.
    store_dir, home, now
        Test overrides. ``store_dir=None`` uses the SciTeX local-state
        cascade (see :func:`_account.account_store._store_path`);
        ``home=None`` uses ``Path.home()``; ``now=None`` uses
        ``time.time()``.
    usage_5h, usage_7d
        Test-injectable per-account utilisation % (name → pct). When
        ``None`` (default, the boot path), each candidate's 5h/7d % is
        read from the bound ``quota-cache.json`` via
        :func:`._quota_rank.account_5h_usage` /
        :func:`._quota_rank.account_7d_usage`. A missing key / missing
        cache reads as "unknown" → per-account degradation.
    reset_7d
        Test-injectable per-account 7d-window reset stamp (name → ISO
        string or epoch seconds). ``None`` (the boot path) reads
        ``reset_at_7d`` from the same cache. This is what separates
        "90%, resets in 6 minutes" (expiring — spend it, it is about to
        be deleted) from "90%, resets in 6 days" (a reserve — leave
        it). Unknown → the reset-unaware behaviour, unchanged.
    quota_cache_path
        Override the quota-cache path (passed through to
        :func:`_account.quota_cache.read_quota_entry`). Ignored when
        the corresponding ``usage_*`` / ``reset_7d`` override is given.
    near_cap_pct
        The 7d % at/above which an account is "near-capped — avoid
        unless no better fresh alternative". Defaults to
        :data:`._quota_rank.NEAR_CAP_7D_PCT` (90). Exposed for tests.
    blocked_5h_pct
        The 5h % at/above which an account is "blocked-now — cannot
        serve requests until its 5h window resets". Defaults to
        :data:`._quota_rank.BLOCKED_5H_PCT` (95). Exposed for tests.
    expiring_horizon_s
        Seconds-to-7d-reset at/below which a near-capped account's
        remainder counts as EXPIRING rather than scarce. Defaults to
        :data:`._quota_rank.EXPIRING_7D_HORIZON_S` (2h) — the knob that
        bounds how long an agent could sit at the cap if it drains the
        remainder before the reset. Exposed for tests / tuning.
    spread_key
        Fleet load-balancing key — pass the AGENT NAME so concurrent
        boots of *different* agents spread across the eligible accounts
        (weighted by 7d headroom) instead of all computing the same
        "best" one. ``None`` / empty keeps the legacy single-winner
        ordering. The same key always maps to the same account while
        quota tiers are unchanged (no churn across restarts).

    Returns
    -------
    str
        The picked stored-account name.

    Raises
    ------
    NoHealthyAccountError
        When no candidate is :attr:`AccountHealth.is_healthy`. The
        message names every candidate's state. NEVER falls back to
        a stale snapshot, and NEVER raised merely because accounts are
        blocked/near-capped (quota is a preference, freshness is the
        gate).
    """
    _home = home if home is not None else Path.home()

    if candidates is None:
        cand_list = _discover_candidates(store_dir, _home)
    else:
        cand_list = list(candidates)

    # Make sure `preferred` is considered even when the caller didn't
    # include it in `candidates` — its absent/expired state still
    # belongs in the error message so the operator sees why we rotated.
    pref = (preferred or "").strip()
    if pref and pref not in cand_list:
        cand_list = [pref, *cand_list]

    if not cand_list:
        raise NoHealthyAccountError(
            "no stored accounts to choose from — run "
            "`sac accounts save <name>` or `sac accounts sync-live` "
            "on the credential-holding host, then retry."
        )

    healths = [
        account_health(name, store_dir=store_dir, home=_home, now=now)
        for name in cand_list
    ]
    by_name = {h.name: h for h in healths}

    fresh = [h for h in healths if h.is_healthy]

    # 3. Nothing token-fresh — fail loud BEFORE any quota logic (a stale
    #    snapshot must never boot, regardless of headroom). The message
    #    pins the probe time and quotes each snapshot's path + raw
    #    expiresAt (INCIDENT 2026-07-10: without this evidence a snapshot
    #    repaired minutes after the probe looked like a false-expired
    #    read and cost the investigation hours).
    if not fresh:
        probe_ts = now if now is not None else time.time()
        raise NoHealthyAccountError(
            f"no healthy stored account (probed at {_iso_utc(probe_ts)}): "
            f"{_format_states(healths)}. Fix: `claude /login` to one of "
            "them, then `sac accounts sync-live`, then restart the agent."
        )

    # Per-account 5h + 7d utilisation for the fresh shortlist. Best-
    # effort: an unknown value (cache absent/stale) degrades per account
    # (never blocks a boot).
    u5: dict[str, float | None] = {
        h.name: account_5h_usage(
            h.name, usage_5h=usage_5h, quota_cache_path=quota_cache_path
        )
        for h in fresh
    }
    u7: dict[str, float | None] = {
        h.name: account_7d_usage(
            h.name, usage_7d=usage_7d, quota_cache_path=quota_cache_path
        )
        for h in fresh
    }
    # WHEN each 7d window resets — the axis that tells quota which is
    # about to be DELETED from quota which is a reserve. Unknown for a
    # cache written before the populator persisted it: every consumer
    # then degrades to the reset-unaware ranking (see is_expiring_7d).
    r7: dict[str, float | None] = {
        h.name: account_7d_reset_at(
            h.name, reset_7d=reset_7d, quota_cache_path=quota_cache_path
        )
        for h in fresh
    }
    probe_now = now if now is not None else time.time()

    # 1. Preferred wins when it is fresh AND not blocked-now (5h) AND
    #    not near-capped (7d); unknown quota keeps it (degrade to
    #    freshness-only). Minimises churn: only rotate off `preferred`
    #    on KNOWN bad quota.
    if pref:
        h = by_name.get(pref)
        if h is not None and h.is_healthy:
            pref_5h = u5.get(pref)
            pref_7d = u7.get(pref)
            blocked_now = pref_5h is not None and pref_5h >= blocked_5h_pct
            # An EXPIRING window is not a scarce one — the same rule the
            # ranking applies (RULE 1). Without this an agent pinned to
            # the very account whose quota is about to evaporate would be
            # rotated OFF it minutes before the reset, which is the waste
            # this change exists to stop, just via the other code path.
            near_capped = (
                pref_7d is not None
                and pref_7d >= near_cap_pct
                and not is_expiring_7d(
                    pref_7d,
                    r7.get(pref),
                    probe_now,
                    horizon_s=expiring_horizon_s,
                )
            )
            if not blocked_now and not near_capped:
                return pref

    # 2. Otherwise the conditional ranking picks among the fresh set —
    #    unblocked beats 5h-blocked, headroom beats near-capped, expiring
    #    capacity is spent before a persisting reserve, and a spread_key
    #    load-balances the winning tier across the fleet. Quota is a
    #    preference, not a hard gate: an all-blocked fleet still returns
    #    the least-bad fresh account.
    return pick_ranked(
        [h.name for h in fresh],
        u5,
        u7,
        reset_7d=r7,
        now=probe_now,
        spread_key=spread_key,
        near_cap_pct=near_cap_pct,
        blocked_5h_pct=blocked_5h_pct,
        expiring_horizon_s=expiring_horizon_s,
    )


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_5h_usage",
    "account_7d_usage",
    "account_health",
    "pick_healthy_account",
]
