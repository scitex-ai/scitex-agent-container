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
* Account-pool Phase 1 — *quota-aware* pick: among the token-fresh
  candidates, prefer the one with the most 7d headroom (lowest 7d
  utilisation %), read from the cached ``quota-cache.json`` the
  apptainer runtime already binds (see :mod:`_account.quota_cache`).
  This is a cheap, *cache-only* read — NO live Claude API call, so it
  never burns account quota at boot. The ``preferred`` account is kept
  when it is fresh and not near-capped (< 90% 7d), to minimise churn;
  otherwise the fresh candidate with the lowest 7d% wins. Accounts at
  or above ~90% 7d are avoided *unless* nothing else is fresh
  (headroom is a preference, not a hard gate). Cap-induced 429s still
  surface from claude in-turn (runtime failover is Phase 2); the
  picker only steers away from *known-stale* auth and *known-capped*
  accounts at boot.
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
from .._account.quota_cache import read_quota_entry
from .._state.account_store import _store_path

# Health states for one account's snapshot. Mirrors
# :class:`_account.creds_sync.Freshness` but anchored on a *name* so the
# picker can return a structured "why I rotated" record to its caller.
_VALID = "VALID"
_EXPIRED = "EXPIRED"
_ABSENT = "ABSENT"

# Account-pool Phase 1: 7d-utilisation threshold above which an account
# is treated as "near-capped — avoid unless no fresh alternative". The
# incident that drove this (2026-07): 2/3 accounts sat at 96-99% weekly
# cap while a 3rd had ~12% headroom, yet agents kept booting onto the
# capped ones. 90% leaves a working margin before the hard weekly wall.
# Headroom is a *preference*, not a hard gate — see
# :func:`pick_healthy_account` (an all-near-capped fleet still boots on
# the least-used fresh account rather than failing).
_NEAR_CAP_PCT = 90.0


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


def _coerce_pct(value: object) -> float | None:
    """Return *value* as a float utilisation %, or ``None`` if not numeric.

    ``bool`` is explicitly rejected (a ``bool`` is an ``int`` in Python)
    so ``True`` never surfaces as ``1.0%`` — mirrors
    :func:`_account.quota_cache._is_number`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def account_7d_usage(
    name: str,
    *,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> float | None:
    """Return one account's cached 7d utilisation %, or ``None``.

    Reads the ``d7`` field of the account's entry in the bound
    ``quota-cache.json`` via :func:`_account.quota_cache.read_quota_entry`
    (which never raises — a missing / stale / unreadable cache, or no
    matching entry, all collapse to ``None``). ``None`` is the caller's
    signal to degrade to freshness-only for *that* account.

    Parameters
    ----------
    name
        The stored-account dirname (e.g. ``ywatanabe-scitex-ai``). The
        cache is keyed on the first dash-segment, so the dirname resolves
        the same entry the a2a-metadata path uses.
    usage_7d
        Test-injectable override: a mapping of account-name → 7d %.
        When provided, it is consulted INSTEAD of the on-disk cache (a
        missing key reads as ``None`` — degrade for that account). This
        mirrors the module's ``store_dir`` / ``home`` / ``now`` override
        idiom so tests need no real ``quota-cache.json`` and no network.
    quota_cache_path
        Override the cache file path passed to
        :func:`read_quota_entry`. Ignored when ``usage_7d`` is given.
    """
    if usage_7d is not None:
        return _coerce_pct(usage_7d.get(name))
    entry = read_quota_entry(account=name, cache_path=quota_cache_path)
    if entry is None:
        return None
    return _coerce_pct(entry.get("d7"))


def _pick_most_headroom(
    fresh: list[AccountHealth],
    usage: dict[str, float | None],
) -> str:
    """Return the fresh account name with the MOST 7d headroom.

    ``fresh`` is the token-fresh shortlist in candidate order (already
    filtered to :attr:`AccountHealth.is_healthy`); ``usage`` maps each
    name to its 7d % or ``None`` (unknown).

    Rule (graceful degradation):

    * Among fresh accounts whose 7d % is *known*, pick the LOWEST
      (most headroom); ties break by candidate order — deterministic so
      two simultaneous boots agree.
    * If NO fresh account has a known usage (cache absent / empty for
      the whole fleet), fall back to the legacy freshness-only behavior:
      the first fresh account in candidate order.

    Accounts with unknown usage therefore never displace a known-headroom
    account, and never crash the pick — they simply degrade to
    freshness-only ordering. Headroom is a *preference*: even when every
    known account is near-capped, this returns the least-used fresh one
    (never raises here — the fail-loud path is "nothing fresh", handled
    by the caller).
    """
    known = [
        (usage[h.name], idx, h.name)
        for idx, h in enumerate(fresh)
        if usage.get(h.name) is not None
    ]
    if known:
        known.sort()
        return known[0][2]
    return fresh[0].name


def pick_healthy_account(
    preferred: str | None,
    *,
    candidates: list[str] | None = None,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
    near_cap_pct: float = _NEAR_CAP_PCT,
) -> str:
    """Return the stored-account name an agent should run on right now.

    Preference order (account-pool Phase 1 — quota-aware)
    -----------------------------------------------------
    1. ``preferred`` (typically ``spec.claude.account``) when its
       snapshot is :attr:`AccountHealth.is_healthy` AND it is NOT
       near-capped — i.e. its cached 7d utilisation is below
       ``near_cap_pct``, OR is unknown (cache absent/stale for it, so we
       degrade to freshness-only and keep it). This minimises churn.
    2. Otherwise, the token-fresh candidate with the MOST 7d headroom
       (lowest cached 7d %). Accounts with an unknown 7d % degrade to
       freshness-only ordering (candidate order) and never displace a
       known-headroom account. Ties break by candidate order (when
       ``candidates`` is explicit) or alphabetically (auto-discovered)
       — deterministic so two simultaneous starts pick the same account.

    Headroom is a *preference*, not a hard gate: when every fresh
    candidate is near-capped, the least-used fresh one is still returned
    (a boot is never blocked on quota — only on there being NOTHING
    token-fresh, which stays fail-loud).

    Parameters
    ----------
    preferred
        The agent's pinned account (``spec.claude.account``). ``None``
        / empty means "no preference" — the picker hands back the
        highest-headroom fresh candidate instead of raising.
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
    usage_7d
        Test-injectable per-account 7d utilisation % (name → pct). When
        ``None`` (default, the boot path), each candidate's 7d % is read
        from the bound ``quota-cache.json`` via
        :func:`account_7d_usage`. A missing key / missing cache reads as
        "unknown" → freshness-only degradation for that account.
    quota_cache_path
        Override the quota-cache path (passed through to
        :func:`read_quota_entry`). Ignored when ``usage_7d`` is given.
    near_cap_pct
        The 7d % at/above which an account is "near-capped — avoid
        unless no fresh alternative". Defaults to :data:`_NEAR_CAP_PCT`
        (90). Exposed for tests.

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
        near-capped (quota is a preference, freshness is the gate).
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

    # Per-account 7d utilisation for the fresh shortlist. Best-effort:
    # an unknown value (cache absent/stale) degrades to freshness-only
    # for that account (never blocks a boot).
    usage: dict[str, float | None] = {
        h.name: account_7d_usage(
            h.name, usage_7d=usage_7d, quota_cache_path=quota_cache_path
        )
        for h in fresh
    }

    # 1. Preferred wins when it is fresh AND has headroom (or its usage
    #    is unknown → degrade to freshness-only, keep it). Minimises
    #    churn: only rotate off `preferred` when we KNOW it is near-capped.
    if pref:
        h = by_name.get(pref)
        if h is not None and h.is_healthy:
            pref_usage = usage.get(pref)
            if pref_usage is None or pref_usage < near_cap_pct:
                return pref

    # 2. Otherwise pick the fresh candidate with the most 7d headroom
    #    (lowest known usage), degrading to freshness-only order when the
    #    cache is unavailable. Headroom is a preference, not a hard gate:
    #    an all-near-capped fleet still returns the least-used fresh one.
    return _pick_most_headroom(fresh, usage)


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_7d_usage",
    "account_health",
    "pick_healthy_account",
]
