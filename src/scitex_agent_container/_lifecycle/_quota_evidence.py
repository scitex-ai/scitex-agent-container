"""Arm the boot-time quota gate — and BUILD the evidence it needs.

The blind-pick gate (:func:`_creds.pick_healthy_account` ``require_quota_evidence``,
raising :class:`_creds.BlindQuotaCacheError`) refuses to boot an agent onto an
account whose quota the cache cannot confirm. The start preflight armed it with::

    require_quota_evidence=quota_cache_present(quota_cache_path)

which guarantees the gate cannot fire in the situation it was built for. The gate
protects against "the cache tells us nothing"; that condition armed it only when a
cache FILE already existed. On a host with NO cache — precisely the blind case —
the gate was DISARMED and the boot proceeded blind. The armed path's auto-refresh
self-repair (operator 2026-08-02: 「refresh quota cache 勝手にやれよ」) sat behind the
same predicate, so the host that most needed its cache built was the one host that
never tried to build it.

MEASURED 2026-08-06, scitex-02 (a newly-provisioned compute node, no quota cron, no
``quota-cache.json``): ``quota_cache_present()`` returned False, the gate was
disarmed, and the pick read "5h=? 7d=?" and landed agent ``figrecipe`` on
``wyusuuke-gmail-com`` at **d7=100.0%**. sac printed ``SUCC``, tmux was alive, the
TUI rendered — and every turn answered "You've hit your weekly limit". Startup
reported success; the agent was functionally dead. Copying ``quota-cache.json`` by
hand fixed it, a repair that leaves no detector.

What is preserved, and what changes
-----------------------------------
The never-block invariant is REAL and stays (see
:func:`_account.quota_cache.quota_cache_present`): a boot is never blocked merely
because this host runs no quota system — a CI box or a quota-cron-less Spartan node
must not be bricked. The defect was never that sac declined to block. It was that
sac went SILENT. So:

1. NO cache → attempt :func:`_account.quota_cache_refresh.refresh_quota_cache` ONCE
   before picking (idempotent, seconds — the same remedy the armed path already
   applies). Evidence built ⇒ arm the gate and pick with it. This alone would have
   prevented scitex-02.
2. The refresh genuinely cannot run (no accounts, no credentials, no network) ⇒
   degrade to freshness-only exactly as before, but emit ONE loud, actionable
   warning naming the picked account. Never proceed wordlessly.
3. Cache already present ⇒ unchanged: armed gate, one auto-refresh on
   :class:`BlindQuotaCacheError`, and the refusal stands if the cache is still blind.

Both preflight call sites route through :func:`pick_with_quota_evidence` rather than
repeating the policy, so the gate cannot be armed one way in one place and another
way in the other.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Stable marker opening the degraded-boot warning. A grep target for the
#: operator and the one token a test can key on without matching the ordinary
#: selection notice.
UNVERIFIABLE_MARKER = "QUOTA UNVERIFIABLE"

__all__ = ["UNVERIFIABLE_MARKER", "pick_with_quota_evidence"]


def pick_with_quota_evidence(
    pick: Callable[[bool], str],
    *,
    agent_name: str,
    quota_cache_path: Path | str | None = None,
    store_dir: Path | None = None,
    log_stream: Any = None,
) -> str:
    """Run *pick* with the quota gate armed whenever evidence can be had.

    Parameters
    ----------
    pick
        Called as ``pick(require_quota_evidence)`` and must return the picked
        stored-account name. A callable rather than a kwargs bundle because the
        two preflight call sites pass different candidate universes; only the
        ARMING policy is shared, and that is the whole point of this helper.
    agent_name
        The booting agent — named in every log line so a fleet restart's output
        attributes each decision.
    quota_cache_path
        Cache override, threaded to the reader, the populator and the picker
        alike so all three agree on which file is under discussion.
    store_dir
        Account store the pick is choosing among. Passed to the populator so an
        on-demand refresh measures THIS pool rather than whatever the default
        cascade happens to discover.
    log_stream
        Follows the preflight's convention: a caller-supplied stream means the
        output is being CAPTURED (the dry-probe suppression path), so it must
        not reach a logger.

    Raises
    ------
    _creds.NoHealthyAccountError
        Propagated from *pick* unchanged — including
        :class:`_creds.BlindQuotaCacheError` when a host that HAS a cache is
        still blind after one refresh.
    """
    from .._account.quota_cache import quota_cache_present

    # A cache that EXISTS is not the same as evidence that is USABLE, and this
    # line used to conflate them.
    #
    # MEASURED 2026-08-17, scitex-hub on scitex-compute-03. Its quota cache was
    # present, well-formed, and 23 HOURS OLD. `quota_cache_present` therefore
    # said True, the armed path trusted it, and the picker read the pinned
    # account's stale percentages — 7d=15% from the previous day — as evidence
    # that the pin was fine. The account was actually at 7d=100%, capped until
    # Aug 22. hub kept the pin and answered "You've hit your weekly limit" on
    # every turn while the restart reported success. Refreshing that one cache
    # was what revived it: the picker then chose a 7d=8% account by itself.
    #
    # So the selector was never wrong. It was fed a day-old number and had no
    # way to know, because nothing in this decision looked at age. Note this is
    # the SAME failure this module's own docstring records for scitex-02 on
    # 2026-08-06 — an agent booting onto a d7=100% account while startup
    # reported success. That fix armed the gate when the cache was ABSENT; a
    # STALE cache walks straight past it, which is why the incident recurred.
    #
    # Treating stale as absent routes it into `_build_evidence_once` — the path
    # that already exists for "we have no evidence, go and get some" — so a
    # refresh happens before the pick rather than a day later. Constitution §2:
    # unknown is not "OK", and a number nobody can vouch for is unknown however
    # confidently it is formatted.
    if _has_fresh_quota_evidence(quota_cache_path):
        return _pick_armed(
            pick,
            agent_name=agent_name,
            quota_cache_path=quota_cache_path,
            store_dir=store_dir,
        )

    blocker = _build_evidence_once(
        agent_name=agent_name,
        quota_cache_path=quota_cache_path,
        store_dir=store_dir,
    )
    if blocker is None:
        return _pick_armed(
            pick,
            agent_name=agent_name,
            quota_cache_path=quota_cache_path,
            store_dir=store_dir,
        )

    picked = pick(False)
    _warn_unverifiable(
        picked,
        agent_name=agent_name,
        blocker=blocker,
        quota_cache_path=quota_cache_path,
        log_stream=log_stream,
    )
    return picked


def _pick_armed(
    pick: Callable[[bool], str],
    *,
    agent_name: str,
    quota_cache_path: Path | str | None,
    store_dir: Path | None,
) -> str:
    """Pick with the gate ARMED, self-repairing a blind cache once.

    AUTO-REFRESH, THEN RE-PICK — operator 2026-08-02: 「refresh quota cache
    勝手にやれよ」. A blind cache told the operator to run `sac accounts
    refresh-quota-cache` and retry BY HAND, and it blocked three of five agents in
    ONE `sac-restart` invocation. sac knows the remedy, and the remedy is
    idempotent and takes seconds — so sac runs it instead of printing it.

    ONE attempt, and ONLY for :class:`BlindQuotaCacheError`. Refusing to boot on
    unverifiable quota (constitution §2: unknown is not 'OK') is unchanged: if the
    cache is STILL blind after a successful refresh, the refusal stands and its
    remedy text is then CORRECT rather than misleading — "the populator is not the
    problem; look for another writer".
    """
    from .._creds import BlindQuotaCacheError

    try:
        return pick(True)
    except BlindQuotaCacheError as blind:
        logger.info(
            "quota cache blind for %r — refreshing it once before refusing",
            agent_name,
        )
        try:
            _refresh(quota_cache_path=quota_cache_path, store_dir=store_dir)
        except Exception:  # stx-allow: fallback (reason: the refresh is a BEST-EFFORT self-repair. If it fails, the operator must see the ORIGINAL blind refusal and its remedy — not a refresh traceback that hides why the boot was refused.)
            raise blind from None
        return pick(True)


def _build_evidence_once(
    *,
    agent_name: str,
    quota_cache_path: Path | str | None,
    store_dir: Path | None,
) -> str | None:
    """Populate a MISSING quota cache. ``None`` when the gate can now be armed.

    Returns the reason the gate must stay disarmed otherwise — prose for the
    warning, so the operator is told what sac actually observed rather than a
    guess. Only ever called when no cache exists.
    """
    from .._account.quota_cache import quota_cache_present
    from .._account.quota_cache_refresh import REASON_NO_ACCOUNTS

    logger.info(
        "no quota cache on this host — building one before picking an account for %r",
        agent_name,
    )
    try:
        result = _refresh(quota_cache_path=quota_cache_path, store_dir=store_dir)
    except Exception as exc:  # stx-allow: fallback (reason: the on-demand build is BEST-EFFORT; a populator that raises must degrade to the documented freshness-only boot, never crash a start that would previously have succeeded.)
        return f"the quota refresh raised {exc.__class__.__name__}: {exc}"

    if not isinstance(result, dict):
        return "the quota refresh returned no result"
    if result.get("reason") == REASON_NO_ACCOUNTS:
        return "no accounts are stored on this host, so there is nothing to measure"

    measured = _as_int(result.get("ok"))
    if measured <= 0:
        # EVERY account failed (no credentials, no network, a lapsed refresh
        # token). The populator still WROTE — see _discard_unusable_cache.
        _discard_unusable_cache(quota_cache_path)
        found = _as_int(result.get("accounts_found"))
        return (
            f"all {found} stored account(s) failed to report usage "
            "(no credentials or no network reachable from here)"
        )
    if not quota_cache_present(quota_cache_path):
        return "the quota refresh reported success but left no readable cache"
    return None


def _discard_unusable_cache(quota_cache_path: Path | str | None) -> None:
    """Remove the EMPTY cache a failed on-demand build just materialised.

    :func:`refresh_quota_cache` writes unconditionally once the store holds any
    account, so a round in which every account fails still publishes
    ``{"accounts": {}}``. That file's mere EXISTENCE is what
    :func:`_account.quota_cache.quota_cache_present` reports, and therefore what
    arms the gate — so leaving it behind converts this host's never-block degrade
    into a PERMANENT hard refusal on every later boot. The self-repair would have
    built the brick.

    Two measured preconditions, never assumptions: the caller only reaches here
    after :func:`quota_cache_present` said the file did not exist, and the unlink
    only runs when :func:`diagnose_quota_cache` observes ``empty`` right now — so
    a populated cache (including one a concurrent boot just wrote) is never
    touched.
    """
    from .._account.quota_cache import diagnose_quota_cache

    state, _entries, path = diagnose_quota_cache(quota_cache_path)
    if state != "empty":
        return
    try:
        path.unlink()
    except OSError:  # stx-allow: fallback (reason: failing to remove the empty cache degrades to today's behaviour — a later boot arms the gate and refuses loudly. A start must not crash over cleanup.)
        logger.warning("could not remove the empty quota cache at %s", path)


def _warn_unverifiable(
    picked: str,
    *,
    agent_name: str,
    blocker: str,
    quota_cache_path: Path | str | None,
    log_stream: Any,
) -> None:
    """Say — once, loudly — that this boot's quota could not be confirmed.

    The never-block invariant means this boot proceeds. It must not proceed
    SILENTLY: on scitex-02 the silence is the whole defect, because every other
    signal (``SUCC``, a live tmux, a rendered TUI) reported success while the
    agent answered "You've hit your weekly limit" on every turn.
    """
    from .._account.quota_cache import diagnose_quota_cache

    _state, _entries, path = diagnose_quota_cache(quota_cache_path)
    text = (
        f"{UNVERIFIABLE_MARKER} — {agent_name}: starting on account {picked} "
        f"WITHOUT confirming it has 5h/7d headroom. No quota cache at {path}, and "
        f"sac could not build one ({blocker}). {picked} may already be at its cap: "
        "the agent will start and report success while every turn answers "
        '"You\'ve hit your weekly limit" (measured 2026-08-06, scitex-02). '
        "Fix: on THIS host run `sac accounts save <name>` (or `sac accounts "
        "sync-live`) if it holds no accounts, then `sac accounts "
        "refresh-quota-cache` — and install its cron so the next boot can see "
        "quota without asking."
    )
    if log_stream is not None:
        print(f"[sac:creds] {text}", file=log_stream)
        return

    from ..cli_pkg._helpers._console import system_msg

    system_msg(text, style="warn")


def _refresh(
    *,
    quota_cache_path: Path | str | None,
    store_dir: Path | None,
) -> dict[str, Any]:
    from .._account.quota_cache_refresh import refresh_quota_cache

    return refresh_quota_cache(cache_path=quota_cache_path, store_dir=store_dir)


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# How old a quota snapshot may be and still count as EVIDENCE for a boot
# decision. Deliberately generous relative to the 5-minute writer cadence
# (`_account.claude_usage._CACHE_TTL_SECONDS`) — this is not "is the cache
# warm", it is "could this number still be true". An hour of drift cannot turn
# a healthy account into a capped one under any realistic burn rate; a day
# demonstrably can, and did.
QUOTA_EVIDENCE_MAX_AGE_S = 3600.0


def _has_fresh_quota_evidence(
    quota_cache_path: Path | str | None,
    *,
    max_age_s: float = QUOTA_EVIDENCE_MAX_AGE_S,
    now: float | None = None,
) -> bool:
    """Is there a quota snapshot RECENT ENOUGH to decide a boot on?

    Three outcomes collapse to two here on purpose, and the collapse is the
    safe direction: absent, unreadable, undated and stale all return False,
    which routes the caller into "go and build the evidence" rather than into
    a silent launch. Only a present, parseable, dated and recent cache is
    evidence.

    ``now`` and ``max_age_s`` are injection seams so the tests can age a cache
    deterministically instead of sleeping or patching the clock.
    """
    import json
    import time

    # `_resolve_cache_path` is the reader's OWN resolver (override -> env ->
    # container bind -> host default). Re-implementing that cascade here is how
    # a freshness check ends up dating a different file than the picker reads.
    from .._account.quota_cache import _resolve_cache_path, quota_cache_present

    if not quota_cache_present(quota_cache_path):
        return False

    # stx-allow: fallback (reason: an unreadable or undated cache is TREATED AS
    # STALE, which is the conservative branch — it triggers a refresh rather
    # than allowing a boot on evidence we cannot date)
    try:
        payload = json.loads(Path(_resolve_cache_path(quota_cache_path)).read_text())
        written_at = float(payload["written_at"])
    except Exception:
        return False

    current = time.time() if now is None else now
    return (current - written_at) <= max_age_s
