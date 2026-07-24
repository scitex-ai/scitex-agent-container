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

The account-health probing and the operator-facing error rendering it
uses live in :mod:`._account_health` (``AccountHealth``,
``account_health``, ``NoHealthyAccountError``); this module is the pure
pick DECISION on top of them.

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
  the freshness-only behavior — never a crash, never a blocked boot —
  UNLESS the caller sets ``require_quota_evidence`` (see below).
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

``require_quota_evidence`` (opt-in, set by the boot preflight) extends
that contract to the QUOTA axis, per constitution §2 — *unknown is a
third state, never silently collapsed into "OK"*. When it is set and
the selected account's cached utilisation is entirely unknown (no 5h
AND no 7d), the pick is BLIND: the cache told us nothing, so we cannot
confirm the account has headroom. Rather than boot an agent onto a
possibly-exhausted account (2026-07-20 incident: an empty cache read
"5h=? 7d=?" and launched scitex-cards on a 7d=100% account), the picker
raises. Freshness stays the token gate; this adds "verifiable quota" as
a boot gate ONLY when the caller asks for it, so library / test callers
keep the graceful-degradation behaviour by default.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Mapping

from ._account_health import (
    AccountHealth,
    NoHealthyAccountError,
    _discover_candidates,
    _format_states,
    _iso_utc,
    account_health,
)
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
from ._spend_policy import (
    POLICY_BURN,
    POLICY_SPREAD,
    validate_7d_policy,
)

# Thresholds live in ._quota_rank (shared with the ranking); the local
# aliases keep this module's public parameter defaults stable.
_NEAR_CAP_PCT = NEAR_CAP_7D_PCT
_BLOCKED_5H_PCT = BLOCKED_5H_PCT


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
    policy: str = POLICY_SPREAD,
    require_quota_evidence: bool = False,
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
       we only rotate off ``preferred`` on *known* bad quota. (With
       ``require_quota_evidence`` a BLIND preferred — no cached 5h AND
       no cached 7d — is instead rotated off, so a pin we cannot see
       does not win over a known-headroom alternative.)
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
    being NOTHING token-fresh, which stays fail-loud). The one
    exception is ``require_quota_evidence`` (below), which turns a
    fully-BLIND pick (no cached quota at all) into a loud failure.

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
    policy
        The 7d spend policy (:mod:`._spend_policy`). ``POLICY_SPREAD``
        (default) is the behaviour above, unchanged. ``POLICY_BURN``
        (opt-in; activation gated on the fleet reconciler) prefers the
        HIGHEST 7d usage among 5h-unblocked fresh accounts, tie-break
        soonest 7d reset — and a near-capped 7d ``preferred`` is a
        reason to STAY (drain it), never to rotate off.
    require_quota_evidence
        Boot-time quota gate (constitution §2 — unknown is not "OK").
        Default ``False`` preserves graceful degradation for library /
        test callers. When ``True`` (the ``sac agents start`` boot
        preflight) a fully-BLIND selection — the picked account has
        NEITHER a cached 5h NOR a cached 7d utilisation — raises
        :class:`NoHealthyAccountError` instead of booting an unverifiable
        account. Blind candidates are also excluded from the ranking while
        any SIGHTED (cached-quota) candidate exists — a blind account can
        never pass the gate, so it must not displace one that can (even a
        near-capped one: least-bad sighted beats unverifiable). The gate
        therefore fires only when the cache is empty for EVERY fresh
        candidate; a fleet with known-but-busy quota still returns
        least-bad.

    Returns
    -------
    str
        The picked stored-account name.

    Raises
    ------
    NoHealthyAccountError
        When no candidate is :attr:`AccountHealth.is_healthy` (the
        message names every candidate's state; NEVER falls back to a
        stale snapshot, and NEVER raised merely because accounts are
        blocked/near-capped — quota is a preference, freshness is the
        gate). Also raised, when ``require_quota_evidence`` is set, if
        the selected account's quota is entirely unknown (a blind pick).
    """
    validate_7d_policy(policy)
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

    picked: str | None = None

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
            # POLICY_BURN inverts the 7d rule: a near-capped pin is a
            # reason to STAY and drain it, never to rotate off.
            if policy == POLICY_BURN:
                near_capped = False
            # CONSTITUTION §2 — a BLIND pin (no cached 5h AND no cached
            # 7d) is NOT "confirmed OK"; under require_quota_evidence do
            # not keep it. Falling through lets the ranking prefer a
            # known-headroom account; if none exists (all blind) the
            # blind-pick gate below turns the pick into a loud failure.
            pref_blind = pref_5h is None and pref_7d is None
            keep_pref = not blocked_now and not near_capped
            if require_quota_evidence and pref_blind:
                keep_pref = False
            if keep_pref:
                picked = pref

    # 2. Otherwise the conditional ranking picks among the fresh set —
    #    unblocked beats 5h-blocked, headroom beats near-capped, expiring
    #    capacity is spent before a persisting reserve, and a spread_key
    #    load-balances the winning tier across the fleet. Quota is a
    #    preference, not a hard gate: an all-blocked fleet still returns
    #    the least-bad fresh account.
    #
    #    Under require_quota_evidence a BLIND candidate (no cached 5h AND
    #    no cached 7d) can never boot — the gate below refuses it — so it
    #    must not displace a SIGHTED one in the ranking either. Without
    #    this restriction the tier order (d7-unknown sorts ahead of
    #    near-capped) hands the gate a blind winner whenever every sighted
    #    account is near-capped, and the boot is refused even though a
    #    verifiable least-bad account exists (2026-07-25 incident: a
    #    cancelled account's usage fetch FAILED → no cache entry → it
    #    outranked two 7d≥90% siblings and blocked the restart).
    if picked is None:
        rank_pool = [h.name for h in fresh]
        if require_quota_evidence:
            sighted = [
                n for n in rank_pool if u5.get(n) is not None or u7.get(n) is not None
            ]
            if sighted:
                rank_pool = sighted
        picked = pick_ranked(
            rank_pool,
            u5,
            u7,
            reset_7d=r7,
            now=probe_now,
            spread_key=spread_key,
            near_cap_pct=near_cap_pct,
            blocked_5h_pct=blocked_5h_pct,
            expiring_horizon_s=expiring_horizon_s,
            policy=policy,
        )

    # 4. BLIND-PICK GATE (constitution §2 — unknown is a third state,
    #    never collapsed into "OK"). Under require_quota_evidence, if the
    #    selected account has NEITHER a cached 5h NOR a cached 7d reading,
    #    the quota cache told us nothing about it and we cannot confirm it
    #    has headroom. Because the ranking above is restricted to sighted
    #    candidates whenever any exist, a blind winner means EVERY fresh
    #    candidate is blind (an empty/absent cache) — the 2026-07-20
    #    incident, where the pick read "5h=? 7d=?" and booted a 7d=100%
    #    account. Refuse rather than boot blind; a fleet with
    #    known-but-busy quota is unaffected.
    if require_quota_evidence and u5.get(picked) is None and u7.get(picked) is None:
        raise NoHealthyAccountError(
            f"quota cache is blind for the selected account {picked!r} "
            "(5h=? 7d=? — no cached utilisation for any fresh candidate), "
            "so the pick cannot be confirmed to have headroom. Refusing to "
            "boot without verifiable quota (constitution: unknown is not "
            "'OK'). Fix: populate the cache on THIS host — run `sac accounts "
            "refresh-quota-cache` (or wait for its cron) — then restart."
        )

    return picked


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_5h_usage",
    "account_7d_usage",
    "account_health",
    "pick_healthy_account",
]
