"""One bounded quota-cache refresh around the pure boot account picker."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .._account.quota_cache_refresh import refresh_quota_cache
from .._creds import (
    BlindQuotaCacheError,
    pick_healthy_account,
)

QuotaRefresher = Callable[..., dict[str, Any]]


def pick_boot_account(
    preferred: str | None,
    *,
    candidates: list[str] | None = None,
    store_dir: Path | None = None,
    now: float | None = None,
    usage_5h: Mapping[str, float] | None = None,
    usage_7d: Mapping[str, float] | None = None,
    quota_cache_path: Path | str | None = None,
    spread_key: str | None = None,
    policy: str,
    require_quota_evidence: bool,
    log_stream: Any = None,
    quota_refresher: QuotaRefresher = refresh_quota_cache,
) -> str:
    """Pick an account, refreshing a blind host quota cache exactly once.

    Expired/absent credentials and every other selection error propagate
    untouched. Test-injected usage maps also retain the picker's pure behavior;
    automatic refresh is only meaningful when production is reading the real
    aggregate cache.
    """
    def _pick() -> str:
        return pick_healthy_account(
            preferred,
            candidates=candidates,
            store_dir=store_dir,
            now=now,
            usage_5h=usage_5h,
            usage_7d=usage_7d,
            quota_cache_path=quota_cache_path,
            spread_key=spread_key,
            policy=policy,
            require_quota_evidence=require_quota_evidence,
        )

    try:
        return _pick()
    except BlindQuotaCacheError:
        if usage_5h is not None or usage_7d is not None:
            raise

    result = quota_refresher(
        store_dir=store_dir,
        cache_path=quota_cache_path,
    )
    stream = log_stream if log_stream is not None else sys.stderr
    ok = int(result.get("ok", 0) or 0)
    found = int(result.get("accounts_found", 0) or 0)
    failed = int(result.get("failed", 0) or 0)
    if ok:
        print(
            "[sac:quota] quota cache had no usable evidence; automatic "
            f"refresh succeeded for {ok}/{found} account(s) "
            f"({failed} failed), retrying selection.",
            file=stream,
        )
    else:
        print(
            "[sac:quota] quota cache had no usable evidence; automatic "
            f"refresh produced no usable entries (found={found}, "
            f"failed={failed}), keeping boot blocked.",
            file=stream,
        )

    # Bounded retry: the canonical picker error remains the final diagnostic
    # if refresh found no accounts, all usage calls failed, or the resulting
    # cache still does not cover a fresh candidate.
    return _pick()


__all__ = ["pick_boot_account"]
