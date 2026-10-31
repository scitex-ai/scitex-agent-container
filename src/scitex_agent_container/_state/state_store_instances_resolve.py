"""Uuid-aware name resolution for ``instances`` rows.

Names are MUTABLE labels (dir-as-SSoT, renames rewrite them, peer hosts
carry stale copies). ``spec.uuid`` is the immutable identity stamped at
create / backfilled first-seen (card ``sac-identity-spec-uuid-20261026``).
This module is the single place that turns one name into one live row:

* 1 live row -> resolve it, no hint.
* 1 live row + stale same-name rows -> resolve the live one AND describe
  the shadow with the exact command that clears it.
* 2+ live claimants -> :class:`DualLiveClaimants`, fail-closed. The
  previous behaviour (pick newest silently) could route a turn to the
  wrong host with no signal; a refusal names every claimant instead.

``live_instance_for_name`` keeps its exact behaviour for the routing
readers — this resolver is the explicit surface operators and diagnosis
paths call when a name is disputed, not a second spelling of the same
read. Staleness here is heartbeat age (the same ``last_heartbeat_at``
clock :mod:`.state_store_gc` sweeps on), never a process probe: this
host cannot signal a peer's pids, and a resolver that refused to judge
a remote row would be unable to answer the very collision it exists for.

``rows`` is injectable so tests drive the pure decision without a store;
the default reads :func:`.state_store_instances.list_active_instances`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .state_store_instances_store import sortable_recency

__all__ = ["DualLiveClaimants", "resolve_live_by_name"]


class DualLiveClaimants(RuntimeError):
    """Two or more live rows claim one name. Fail-closed by design.

    Carries ``claimants`` (the live row dicts, newest first) so handlers
    can name every host/instance without re-querying.
    """

    def __init__(self, name: str, claimants: list[dict[str, Any]]) -> None:
        self.name = name
        self.claimants = list(claimants)
        super().__init__(_refusal_text(name, claimants))


def _heartbeat_age_s(row: dict[str, Any], *, now: float) -> float | None:
    """Age of ``last_heartbeat_at`` in seconds, or None if unjudgeable.

    Twin of :mod:`.state_store_gc`'s ``_heartbeat_seconds`` read the other
    way (age rather than epoch): same format, same tolerance — a missing
    or malformed stamp is not evidence of anything.
    """
    raw = row.get("last_heartbeat_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = (
            datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError):
        return None
    return now - stamp


def _describe(row: dict[str, Any]) -> str:
    spec = row.get("spec_uuid") or "unstamped"
    return (
        f"host={row.get('host', '?')} id={row.get('id', '?')} "
        f"started_at={row.get('started_at', '?')} spec_uuid={spec}"
    )


def _refusal_text(name: str, claimants: list[dict[str, Any]]) -> str:
    lines = "\n  ".join(_describe(r) for r in claimants)
    return (
        f"refusing: {len(claimants)} live instances claim name {name!r} "
        f"(fail-closed — routing one turn to the wrong host is worse "
        f"than routing none):\n  {lines}\n"
        f"Stop all but one (`sac agents stop {name}` on the losing "
        f"host(s) after verifying which is live), then retry. "
        f"`sac agents forget {name}` cannot do this: it refuses while "
        f"any live row exists."
    )


def _hint_text(
    name: str, live: dict[str, Any], stale: list[dict[str, Any]]
) -> str:
    stale_hosts = sorted({str(r.get("host", "?")) for r in stale})
    lines = "\n  ".join(_describe(r) for r in stale)
    live_uuid = live.get("spec_uuid")
    divergent = [
        r for r in stale if r.get("spec_uuid") not in (None, live_uuid)
    ]
    if live_uuid and divergent:
        identity = (
            f"spec identity DIVERGED: the live row carries "
            f"spec_uuid={live_uuid}, but {len(divergent)} stale row(s) "
            f"carry a different one — same name, different specs. "
            f"Reconcile the spec files via the fleet sync, not by "
            f"renaming."
        )
    elif live_uuid:
        identity = (
            f"same spec_uuid={live_uuid} on all rows: leftovers of "
            f"restarts/stops, not a fork."
        )
    else:
        identity = (
            "rows are UNSTAMPED (pre-backfill): run the spec backfill "
            "(`backfill_spec_uuids` over the agents dir, fleet stopped) "
            "so the next collision can be keyed on identity."
        )
    return (
        f"name {name!r} resolves to the live row on "
        f"{_describe(live)}; {len(stale)} stale row(s) shadow it:\n  "
        f"{lines}\n{identity}\n"
        f"On each stale host ({', '.join(stale_hosts)}) run "
        f"`sac store clean` to reap the dead rows (host-scoped sweep), "
        f"then re-check."
    )


def resolve_live_by_name(
    name: str,
    *,
    rows: list[dict[str, Any]] | None = None,
    now: float | None = None,
    heartbeat_stale_seconds: int = 300,
) -> "tuple[dict[str, Any] | None, str | None]":
    """Resolve ``name`` to ``(live_row, hint)``.

    * No same-name active row -> ``(None, None)``.
    * Exactly one -> ``(row, None)``.
    * One live + stale rows -> ``(live, hint)`` where the hint names the
      stale rows, their spec-uuid (mis)alignment, and the exact cleanup
      command per stale host.
    * Two or more LIVE rows (fresh heartbeats on several) -> raises
      :class:`DualLiveClaimants`. Fail-closed: no silent newest-pick.

    \"Live\" for a non-newest row means a heartbeat newer than
    ``heartbeat_stale_seconds`` (default 300, the gc sweep's own
    threshold). Older or unstamped non-newest rows are stale: the newest
    write wins and the rest are shadow, which is exactly the restart /
    crash residue the sweep reaps a step later.
    """
    if not name:
        return None, None
    if rows is None:
        from .state_store_instances import list_active_instances

        rows = list_active_instances()
    candidates = [r for r in rows if r.get("name") == name]
    if not candidates:
        return None, None
    ordered = sorted(candidates, key=sortable_recency, reverse=True)
    live = ordered[0]
    if len(ordered) == 1:
        return live, None
    moment = time.time() if now is None else now
    stale: list[dict[str, Any]] = []
    live_claimants: list[dict[str, Any]] = [live]
    for row in ordered[1:]:
        age = _heartbeat_age_s(row, now=moment)
        if age is not None and age <= heartbeat_stale_seconds:
            live_claimants.append(row)
        else:
            stale.append(row)
    if len(live_claimants) > 1:
        raise DualLiveClaimants(name, live_claimants)
    return live, _hint_text(name, live, stale)
