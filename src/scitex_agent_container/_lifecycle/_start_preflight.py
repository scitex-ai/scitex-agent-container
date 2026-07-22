"""Pre-flight helpers for ``agent_start`` — liveness, account rotation,
strict-drift resolution, and launch-time spec-source drift.

Extracted from ``_start.py`` (split for the 512-line module limit).
``_start.agent_start`` imports these; behaviour is unchanged.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import AgentConfig


def _verify_real_liveness(
    config: AgentConfig,
    runtime,
    *,
    instances_oracle: Callable[[], list[dict]] | None = None,
) -> bool:
    """Return True iff the agent is *demonstrably* running on this host.

    The pre-fix call site treated ``registry.exists(name) and
    runtime.is_running(config)`` as the already-running signal. Both
    are necessary but not sufficient:

      * ``registry.exists`` is a JSON file on disk — a forced ``rm``,
        a stale entry from a prior boot, or a crash-during-write leaves
        the file behind even though no agent is running.
      * ``runtime.is_running`` checks the per-runtime PID file with
        ``os.kill(pid, 0)`` — on a Linux PID-wraparound the same pid
        can belong to a completely unrelated process and the probe
        returns True.

    Either of those false positives causes the no-op branch in
    :func:`agent_start` to swallow a real start request silently and
    return rc=0. The cross-host ``instances`` table is the third
    independent signal — it is written by ``record_local_instance``
    inside the *real* start path and removed by ``agent_stop`` /
    ``cleanup_stale``. We require an active row before treating the
    agent as already-running. If the row is absent, the registry/PID
    pair is inconsistent → fall through to a real start instead of
    the silent no-op.

    The ``instances_oracle`` seam is the no-mocks knob for tests; it
    defaults to a host-unfiltered :func:`state_db.list_active_instances`
    call (we want ANY active row for the name, not just rows on the
    current host — handover may have moved it).
    """
    if instances_oracle is None:
        from .._state.state_db import list_active_instances as _default

        def instances_oracle():  # type: ignore[no-redef]
            # Explicit host=None so the call is unambiguous and the
            # fixture-isolated test reads through the same module.
            return _default(host=None)

    try:
        rows = instances_oracle()
    except Exception:
        # stx-allow: fallback (reason: a missing/locked state.db must
        # not block the start path; degrade to "no liveness evidence"
        # which causes the caller to launch fresh rather than silently
        # no-op)
        return False
    for row in rows or ():
        if row.get("name") == config.name:
            return True
    return False


def _resolve_strict_drift(strict_drift: bool | None) -> bool:
    """Resolve effective strict-drift mode (arg wins, else env).

    ``strict_drift=True/False`` from ``--strict-drift`` takes priority.
    ``None`` falls back to ``SAC_STRICT_DRIFT`` (``1``/``true``/``yes``
    → strict). Read through the sac env helper so either prefix works.
    """
    if strict_drift is not None:
        return strict_drift
    from .._env import getenv as _sac_env

    raw = (_sac_env("STRICT_DRIFT", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _slug_of_credentials_file(path: Path) -> str:
    """Return the account slug for a ``.credentials.json`` path.

    The slug is the file's PARENT directory name — the fleet layout is
    ``~/.scitex/agent-container/accounts/<slug>/.credentials.json`` and
    ``<slug>`` is exactly the stored-account name the quota-aware picker
    (:func:`_creds.pick_healthy_account`) keys off (see
    :func:`_creds._pick_healthy.account_health`).
    """
    return path.parent.name


def _rotate_among_credentials_files(
    config: AgentConfig,
    paths: list[str],
    *,
    log_stream: Any = None,
    now: float | None = None,
    usage_5h: Mapping[str, float] | None = None,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> None:
    """Pick ONE credentials file from the pool, quota-aware, and bind it.

    ``paths`` is the account POOL (``spec.claude.credentials_files``, or
    the singular ``spec.claude.credentials_file`` treated as a 1-element
    pool). Each entry's account SLUG is its parent-dir name. We hand the
    slug list to :func:`_creds.pick_healthy_account` so the SAME
    quota-conditional pick used for named accounts chooses among exactly
    the listed accounts — token freshness as the hard gate, then avoid
    5h-blocked and 7d-near-capped accounts, then LOAD-BALANCE the
    winning tier across the fleet (``spread_key=config.name`` →
    7d-headroom-weighted rendezvous hash, so a bulk restart spreads
    agents over the healthy accounts instead of stacking them all onto
    one; see :mod:`_creds._quota_rank`). The pool is then collapsed down
    to the picked entry by writing it into
    ``config.claude.credentials_file`` — the field every downstream auth
    path (``runtimes._apptainer_auth.auth_argv`` /
    ``credentials_file_bind``) already resolves. Downstream binding is
    therefore UNCHANGED; this only decides WHICH file it binds.

    ``preferred`` (churn minimisation) is ONLY a genuine pin: the spec's
    ``claude.account`` when it is one of the listed slugs, else the slug
    of the currently-effective ``credentials_file`` when it is listed.
    The first listed entry is deliberately NOT treated as preferred —
    that made every fleet agent "prefer" the same account and defeated
    the spread (2026-07 ``sac-restart --all-running`` incident).

    Health probing reads each slug's snapshot from the pool's common
    parent-of-parent directory (``store_dir``) so the freshness/quota
    check inspects the EXACT listed files — this works for the fleet
    ``accounts/`` layout and for custom locations alike. When the entries
    span different parent dirs, ``store_dir`` falls back to the default
    SciTeX account-store cascade.

    Fail-loud: when NO listed entry has a usable (non-expired) snapshot,
    :class:`_creds.NoHealthyAccountError` propagates (agent NOT started).
    Back-compat: a 1-element pool whose one snapshot is healthy resolves
    to that exact file (no-op — ``credentials_file`` unchanged, no log).
    """
    from .._account.quota_cache import quota_cache_present
    from .._account.quota_cache_refresh import refresh_quota_cache
    from .._creds import (
        POLICY_BURN,
        BlindQuotaCacheError,
        pick_healthy_account,
        resolve_7d_policy,
    )

    entries: list[tuple[str, Path]] = []
    grandparents: set[str] = set()
    for raw in paths:
        p = Path(str(raw)).expanduser()
        entries.append((_slug_of_credentials_file(p), p))
        grandparents.add(str(p.parent.parent))
    slugs = [slug for slug, _ in entries]

    # Common parent-of-parent = the account store dir. When every listed
    # file lives under the same dir (the fleet layout), pass it so the
    # health probe reads the EXACT listed files; otherwise degrade to the
    # default store cascade (store_dir=None).
    store_dir: Path | None = (
        entries[0][1].parent.parent if len(grandparents) == 1 else None
    )

    # The 7d spend policy (env: SAC_CREDS_7D_POLICY). Fail-loud on an
    # invalid value — an operator who asked for a policy must never
    # silently get another. Default stays "spread": burn-to-zero is
    # GATED on the fleet reconciler (see _creds._spend_policy).
    policy = resolve_7d_policy()

    claude = config.claude
    # Preferred = a GENUINE pin only: the spec's named account when it is
    # one of the listed slugs, else the currently-effective
    # credentials_file's slug when it is listed. NOT the first listed
    # entry — see the docstring (fleet-stacking incident). Under
    # POLICY_BURN the prior-slug churn-minimisation is dropped too:
    # keeping the previously-bound file would freeze the pool and defeat
    # draining the near-cap bucket (a named spec pin still holds).
    account = str(getattr(claude, "account", "") or "").strip()
    prior = str(getattr(claude, "credentials_file", "") or "").strip()
    prior_slug = _slug_of_credentials_file(Path(prior)) if prior else ""
    if account in slugs:
        preferred: str | None = account
    elif prior_slug in slugs and policy != POLICY_BURN:
        preferred = prior_slug
    else:
        preferred = None

    def _pick() -> str:
        return pick_healthy_account(
            preferred,
            candidates=slugs,
            store_dir=store_dir,
            now=now,
            usage_5h=usage_5h,
            usage_7d=usage_7d,
            quota_cache_path=quota_cache_path,
            spread_key=config.name,
            policy=policy,
            # Boot gate (constitution §2): on a host that HAS a quota cache, a
            # fully-BLIND pick means the populator failed (empty/stale cache) —
            # fail loud rather than land the agent on an unverifiable, possibly
            # quota-exhausted account (2026-07-20 incident). A host with NO
            # cache (fresh install / CI / quota-cron-less Spartan node) still
            # degrades to freshness-only and boots — the never-block invariant.
            require_quota_evidence=quota_cache_present(quota_cache_path),
        )

    try:
        picked = _pick()
    except BlindQuotaCacheError as blind:
        # AUTO-REFRESH, THEN RE-PICK — operator 2026-08-02:
        # 「refresh quota cache 勝手にやれよ」. A blind cache told the operator
        # to run `sac accounts refresh-quota-cache` and retry BY HAND, and it
        # blocked three of five agents in ONE `sac-restart` invocation. sac
        # knows the remedy, and the remedy is idempotent and takes seconds —
        # so sac runs it instead of printing it.
        #
        # ONE attempt, and ONLY for BlindQuotaCacheError. Refusing to boot on
        # unverifiable quota (constitution: unknown is not 'OK') is unchanged:
        # if the cache is STILL blind after a successful refresh, the refusal
        # stands and its remedy text is then CORRECT rather than misleading —
        # "the populator is not the problem; look for another writer".
        logger.info(
            "quota cache blind for %r — refreshing it once before refusing",
            config.name,
        )
        try:
            refresh_quota_cache(cache_path=quota_cache_path)
        except Exception:  # stx-allow: fallback (reason: the refresh is a BEST-EFFORT self-repair. If it fails, the operator must see the ORIGINAL blind refusal and its remedy — not a refresh traceback that hides why the boot was refused.)
            raise blind from None
        picked = _pick()

    picked_path = next(p for slug, p in entries if slug == picked)
    claude.credentials_file = str(picked_path)

    if str(picked_path) == prior:
        return  # 1-element / already-selected pool — no change, no log.

    from .._creds import (
        account_5h_usage,
        account_7d_reset_at,
        account_7d_usage,
        audit_candidates,
        pick_audit_parts,
    )

    # EVERY ranking input, per candidate — the pick must be auditable
    # (operator 2026-07-17: the notice named criteria but not inputs, so
    # a reasoned pick was indistinguishable from a lucky one). Reads the
    # same caches the pick read; never a token.
    u5 = {
        s: account_5h_usage(s, usage_5h=usage_5h, quota_cache_path=quota_cache_path)
        for s in slugs
    }
    u7 = {
        s: account_7d_usage(s, usage_7d=usage_7d, quota_cache_path=quota_cache_path)
        for s in slugs
    }
    r7 = {s: account_7d_reset_at(s, quota_cache_path=quota_cache_path) for s in slugs}
    records = audit_candidates(slugs, u5, u7, reset_7d=r7, now=now)

    def fmt(v: float | None) -> str:
        return "?" if v is None else f"{v:.0f}%"

    rationale = (
        "token-fresh gate, 5h-blocked excluded, then HIGHEST 7d usage — "
        "burn the perishable weekly bucket to zero — tie-break soonest "
        "7d reset"
        if policy == POLICY_BURN
        else "token-fresh gate, avoids 5h-blocked and 7d-near-capped "
        "accounts, load-balanced per agent by 7d headroom; time-to-reset "
        "counts only within the 2h expiring horizon"
    )
    ranking = "\n".join(f"      {part}" for part in pick_audit_parts(records))
    headline = (
        f"{config.name}: account {picked} "
        f"(5h={fmt(u5.get(picked))} 7d={fmt(u7.get(picked))})"
    )
    detail = (
        f"  file:          {picked_path}\n"
        f"  policy={policy} — {rationale}\n"
        f"  ranking inputs:\n{ranking}"
    )
    # A caller-supplied stream means the notice is being CAPTURED to be
    # discarded (preflight_from_config_path's dry-probe suppression), so it
    # must not reach a logger at all.
    if log_stream is not None:
        print(f"[sac:creds] {headline}\n{detail}", file=log_stream)
        return

    from ..cli_pkg._helpers._console import system_msg

    system_msg(headline, style="info")
    system_msg(detail, style="dim")


def _rotate_to_healthy_account(
    config: AgentConfig,
    *,
    log_stream: Any = None,
    now: float | None = None,
    usage_5h: Mapping[str, float] | None = None,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
) -> None:
    """Rotate the agent's credential to a healthy stored account.

    CREDS-PHASE1 wiring. Two account-pool entry points, checked in order:

    1. **Account POOL** — ``spec.claude.credentials_files`` (plural), or
       the singular ``spec.claude.credentials_file`` treated as a
       1-element pool. When non-empty, delegate to
       :func:`_rotate_among_credentials_files`: derive each entry's
       account slug (parent-dir name), let :func:`_creds.
       pick_healthy_account` choose the quota-aware winner among exactly
       the listed accounts, and collapse the pool to the picked file via
       ``config.claude.credentials_file``. THIS is the wiring that makes
       the quota-aware pick (PR #583/#584) affect ``credentials_file``-
       pinned fleet agents — previously such agents bypassed the pick
       entirely (this function returned early on empty ``account``).

    2. **Named account** — ``spec.claude.account`` non-empty. Keep the
       pinned account when its snapshot is healthy AND its quota is not
       known-bad (5h-blocked / 7d-near-capped); otherwise rotate
       ``config.claude.account`` to the ranked fresh account
       (load-balanced per agent — ``spread_key=config.name``). For an
       unpinned agent (no pool, no account) the runtime continues to
       bind the host's live ``.credentials.json`` untouched.

    In every case a total absence of a usable (non-expired) snapshot
    raises :class:`_creds.NoHealthyAccountError` (fail loud, no silent
    stale-token launch). See :mod:`scitex_agent_container._creds.
    _pick_healthy` for the health model. The ``now`` / ``usage_5h`` /
    ``usage_7d`` / ``quota_cache_path`` params are the same
    test-injection seams ``pick_healthy_account`` exposes; production
    passes ``None``.
    """
    claude = getattr(config, "claude", None)

    # 1. Account POOL (plural, or the singular treated as a 1-element pool).
    cred_files = list(getattr(claude, "credentials_files", []) or [])
    single = str(getattr(claude, "credentials_file", "") or "").strip()
    pool = cred_files if cred_files else ([single] if single else [])
    if pool:
        _rotate_among_credentials_files(
            config,
            pool,
            log_stream=log_stream,
            now=now,
            usage_5h=usage_5h,
            usage_7d=usage_7d,
            quota_cache_path=quota_cache_path,
        )
        return

    # 2. Named account (legacy CREDS-PHASE1 path).
    pinned = getattr(claude, "account", "") or ""
    if not pinned:
        return  # unpinned agent — host live OAuth, untouched.

    from .._account.quota_cache import quota_cache_present
    from .._creds import pick_healthy_account, resolve_7d_policy

    policy = resolve_7d_policy()
    picked = pick_healthy_account(
        pinned,
        now=now,
        usage_5h=usage_5h,
        usage_7d=usage_7d,
        quota_cache_path=quota_cache_path,
        spread_key=config.name,
        policy=policy,
        # Boot gate (constitution §2): a blind-quota pin fails loud on a host
        # that HAS a cache (populator failure); a cache-less host degrades and
        # boots (never-block invariant). See quota_cache_present.
        require_quota_evidence=quota_cache_present(quota_cache_path),
    )
    if picked == pinned:
        return  # pinned is healthy — no rotation, no log line.

    config.claude.account = picked
    notice = (
        f"{config.name}: rotated account {pinned} -> {picked} "
        f"(policy={policy}; {pinned} stale, 5h-blocked, or outranked)"
    )
    if log_stream is not None:
        print(f"[sac:creds] {notice}", file=log_stream)
        return

    from ..cli_pkg._helpers._console import system_msg

    system_msg(notice, style="info")


def _check_spec_source_drift_at_launch(
    config_path: str, agent_name: str, strict_drift: bool | None
) -> None:
    """Run the launch-time drift check; warn loud (or block if strict).

    Fully guarded: the underlying check never raises except the
    deliberate strict-mode :class:`SpecSourceDriftError`. We let that
    propagate (the CLI / caller turns it into a non-zero exit); any
    other unexpected failure here is swallowed so a launch is never
    crashed by the drift guard.
    """
    from .._drift import SpecSourceDriftError, warn_if_spec_source_drifted

    strict = _resolve_strict_drift(strict_drift)
    try:
        warn_if_spec_source_drifted(config_path, agent=agent_name, strict=strict)
    except SpecSourceDriftError:
        # Deliberate strict-mode block — propagate so the caller exits
        # non-zero. This is the ONE thing this guard is allowed to raise.
        raise
    except Exception:  # stx-allow: fallback (reason: the drift guard must NEVER crash a launch; any unexpected error degrades to "no check ran" and the agent proceeds)
        traceback.print_exc()
