"""One-shot peer freshness sweep — orchestrates :mod:`_mutual_watch`.

The pure decision lives in :mod:`_mutual_watch`. This module owns the
side-effects: enumerate peers, run the check, persist alerts into
``state_db.structural_alerts``, and resolve any prior-active alerts
when a peer recovers.

Designed to be called from two places:

  * The heartbeat loop's tick — every N beats the observer agent
    sweeps its declared peers.
  * A standalone CLI ``sac fleet watch`` (future) that one-shots a
    sweep over the registry.

Keeping the sweep stateless (each call enumerates peers fresh,
resolves stale rows, and emits alerts) means we never have to track
"which alerts I already fired" in-process — the DB is the only
source of truth and the upsert semantics in
:func:`state_db_alerts.record_alert` handle dedup.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

from .._runners._session_state import DEFAULT_STATE_ROOT, state_dir_for
from .._state import state_db_alerts as _alerts
from ._mutual_watch import (
    KIND_STALE_HEARTBEAT,
    KIND_STALE_SESSION_JSONL,
    StalePeerAlert,
    WatchConfig,
    check_peer_freshness,
    load_watch_config,
)

log = logging.getLogger(__name__)

# Both kinds the watch can emit — used to resolve any leftover
# active row when a peer recovers (every kind that did not fire
# THIS sweep is clear → resolve it).
_ALL_KINDS = (KIND_STALE_HEARTBEAT, KIND_STALE_SESSION_JSONL)


def sweep_peers(
    *,
    observer: str,
    peer_names: Iterable[str],
    state_root: Path | None = None,
    now: float | None = None,
    config: WatchConfig | None = None,
    db_path: Path | None = None,
) -> list[StalePeerAlert]:
    """Run :func:`check_peer_freshness` against every peer; persist alerts.

    Returns the flat list of alerts emitted this sweep (one per
    failing check; same peer can produce two kinds). Healthy peers
    contribute nothing to the list, AND any prior-active alert for
    that ``(observer, peer, kind)`` is resolved — so a peer that
    recovers between sweeps does not stay flagged forever.

    Failure isolation: a per-peer exception (e.g. permission denied
    reading the peer's state dir) is logged but does NOT stop the
    sweep. The mutual-watch is observability, not the critical
    path — one broken neighbour must not blind the observer to all
    its peers.

    ``state_root`` defaults to :data:`_session_state.DEFAULT_STATE_ROOT`
    so the sweep lives in the same layout the runner writes to.
    """
    cfg = config or load_watch_config()
    ts = float(now) if now is not None else time.time()
    root = state_root or DEFAULT_STATE_ROOT

    emitted: list[StalePeerAlert] = []
    for peer in peer_names:
        if peer == observer:
            # Skip self — an agent watching itself does not surface
            # the mutual-monitoring signal the spec asks for.
            continue
        peer_dir = state_dir_for(peer, root=root)
        try:
            alerts = check_peer_freshness(
                observer=observer,
                peer=peer,
                peer_state_dir=peer_dir,
                now=ts,
                config=cfg,
            )
        except Exception as exc:  # stx-allow: fallback (reason: observability sweep — one peer's read failure must not block the rest of the sweep; logged at WARNING)
            log.warning("mutual-watch sweep: peer %r raised %s", peer, exc)
            continue
        fired_kinds: set[str] = set()
        for alert in alerts:
            fired_kinds.add(alert.kind)
            try:
                _alerts.record_alert(
                    observer=alert.observer,
                    peer=alert.peer,
                    kind=alert.kind,
                    evidence={
                        **alert.evidence,
                        "age_seconds": alert.age_seconds,
                        "threshold_s": alert.threshold_s,
                    },
                    now=ts,
                    db_path=db_path,
                )
            except Exception as exc:  # stx-allow: fallback (reason: DB write best-effort during observability sweep; logged at WARNING)
                log.warning(
                    "mutual-watch: record_alert(%s, %s, %s) failed: %s",
                    alert.observer,
                    alert.peer,
                    alert.kind,
                    exc,
                )
            emitted.append(alert)
        # Resolve any kind that did NOT fire this sweep — the peer
        # has recovered for that check. Idempotent: resolving a
        # never-fired row is a no-op.
        for kind in _ALL_KINDS:
            if kind in fired_kinds:
                continue
            try:
                _alerts.resolve_alert(
                    observer=observer,
                    peer=peer,
                    kind=kind,
                    now=ts,
                    db_path=db_path,
                )
            except Exception as exc:  # stx-allow: fallback (reason: DB write best-effort during observability sweep; logged at WARNING)
                log.warning(
                    "mutual-watch: resolve_alert(%s, %s, %s) failed: %s",
                    observer,
                    peer,
                    kind,
                    exc,
                )
    return emitted


__all__ = ["sweep_peers"]
