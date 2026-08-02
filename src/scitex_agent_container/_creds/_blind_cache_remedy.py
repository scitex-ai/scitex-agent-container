"""The operator-facing remedy line for a BLIND credential pick.

Split out of ``_pick_healthy.py`` to keep that module under the per-file line
cap; it is a pure message builder — it takes a diagnosis and returns prose,
sharing no state with the ranking logic it used to sit beside.

Every branch reports only what :func:`.._account.quota_cache.diagnose_quota_cache`
actually OBSERVED, and always names the file consulted.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["_blind_cache_remedy"]


def _blind_cache_remedy(quota_cache_path: Path | str | None) -> str:
    """The remedy line for a blind pick — chosen by WHY the cache is blind.

    Several distinct causes reach the blind gate and they need different
    instructions; naming only the refresh command sent an operator whose cache
    holds ZERO entries into a loop, because on a host with no stored accounts
    that command finds nothing to refresh and changes nothing.

    Every branch below reports only what :func:`diagnose_quota_cache` actually
    OBSERVED, and always names the file consulted. The previous version branched
    on ``entry_count == 0`` — which is also what an absent, unreadable or
    malformed cache returns — and then asserted a cause it had not measured
    ("the cache exists but holds ZERO account entries, so the populator has
    never written a successful one"). On 2026-07-29 the operator saw exactly
    that while the cron populator was writing three accounts every five minutes,
    re-ran the refresh twice on its advice, and nothing changed: a remedy naming
    a DIRECTION as if it were the truth costs more than one naming nothing.
    """
    from .._account.quota_cache import diagnose_quota_cache

    state, entries, path = diagnose_quota_cache(quota_cache_path)
    where = f"(read from {path})"
    if state == "absent":
        return (
            f"Cause: NO quota cache exists at the path this pick consulted "
            f"{where}. Fix: run `sac accounts refresh-quota-cache` on THIS "
            "host and READ ITS OUTPUT — if it reports no accounts stored "
            "(exit 3) it cannot help, and the real fix is `sac accounts save "
            "<name>` (or `sac accounts sync-live`) here first."
        )
    if state == "unreadable":
        return (
            f"Cause: the quota cache exists but could NOT be read {where} — a "
            "permissions or mount problem, not a populator problem. Re-running "
            "the refresh will not fix it. Fix: check ownership/mode on that "
            "file (the populator writes it 0600 as the invoking user)."
        )
    if state == "malformed":
        return (
            f"Cause: the quota cache exists but is not valid cache JSON {where} "
            "— truncated or hand-edited, NOT an unrun populator. Fix: delete "
            "that file and run `sac accounts refresh-quota-cache` to rewrite "
            "it (the writer is tmp+rename atomic, so a partial file means "
            "something outside the populator wrote there)."
        )
    if state == "empty":
        return (
            f"Cause: the quota cache holds ZERO account entries {where}, so no "
            "populator run has stored one. Fix: run `sac accounts "
            "refresh-quota-cache` on THIS host and READ ITS OUTPUT — if it "
            "reports no accounts stored (exit 3) it cannot help, and the real "
            "fix is `sac accounts save <name>` (or `sac accounts sync-live`) "
            "here first, then re-run the refresh. If that refresh POPULATES "
            "the file and a later pick is blind AGAIN, the populator is not "
            "the problem — look for another writer on this path before "
            "re-running the refresh a third time."
        )
    return (
        f"Cause: the quota cache holds {entries} account entr"
        f"{'y' if entries == 1 else 'ies'} {where}, but none of them covers a "
        "fresh candidate — it is STALE, or was written for a different account "
        "set than this agent's credentials pool. Fix: compare those entry keys "
        "against the pool, then run `sac accounts refresh-quota-cache` (or "
        "wait for its cron) and restart."
    )
