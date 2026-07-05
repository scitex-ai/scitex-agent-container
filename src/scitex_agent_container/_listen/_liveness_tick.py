"""Liveness-tick reconciler: ALARM on a stuck OPEN card (alarm-engine producer).

Card ``sac-card-anchored-stop-reconciler``. PRINCIPLE (operator): do not
trust agentic flow; harness it deterministically. The card store CARD
is the anchor of truth — a card stays OPEN until explicitly resolved.
This is the deterministic reconciler that runs INSIDE ``sac listen``,
reads cards (truth) vs. real agent activity, and ALARMS on a mismatch so
a stuck/crashed agent can never sit silent.

SEPARATION OF CONCERNS: **sac only DETECTS and EMITS.** sac does NOT
write to the card store. We emit an anomaly event on sac's own
``scitex_agent_container.hooks`` entry-point event bus; a downstream
card-store consumer (registered separately, on its side) turns it into a
card record + operator push. Nothing here writes the card store.

BUS-GROUP TRANSITION (backward-compat): this bus was originally named
``scitex_todo.hooks`` and scitex-todo still registers its consumer / emits
card-events on that legacy group. So we DUAL-SUPPORT during the
transition — sac loads consumers from BOTH the new
``scitex_agent_container.hooks`` group AND the legacy ``scitex_todo.hooks``
group (see :data:`HOOKS_ENTRY_POINT_GROUP` /
:data:`LEGACY_HOOKS_ENTRY_POINT_GROUP`), so sac's emits still reach
scitex-todo's consumer and sac still receives scitex-todo's card-events.
The legacy group is removed once scitex-todo migrates to the new name.

Bind-safety (cards ``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``): the only blocking IO — reading
``tasks.yaml`` + each owner's ``session.jsonl`` + the active-instances
registry — runs through :func:`_off_loop.run_blocking_or`, so a
slow/locked read can NEVER starve uvicorn's bind or the running server.

This module is the IO + emit + loop glue; the pure reconcile rule lives
in :mod:`_liveness_tick_detect` and is re-exported here.

Event shape (locked proposal)::

    {"agent": str, "card_id": str, "reason": str,
     "severity": str, "ts": float}

``reason`` ∈ {"owner-not-live", "owner-idle"}; ``severity`` scales with
staleness ("warning" → "critical").
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ._liveness_tick_detect import (  # re-exported public surface
    AgentLiveness,
    StuckCard,
    find_stuck_cards,
    open_card_owners,
)

logger = logging.getLogger(__name__)

# --- env knobs (conservative defaults to avoid false alarms) ----------------
ENV_DISABLED = "SAC_LIVENESS_TICK_DISABLED"
ENV_INTERVAL_S = "SAC_LIVENESS_TICK_INTERVAL_S"
ENV_STALE_S = "SAC_LIVENESS_TICK_STALE_S"
ENV_RENOTIFY_S = "SAC_LIVENESS_TICK_RENOTIFY_S"

DEFAULT_INTERVAL_S = 120.0
DEFAULT_STALE_S = 900.0
DEFAULT_RENOTIFY_S = 3600.0

# The entry-point event bus sac emits an anomaly onto. A downstream
# card-store consumer registers here (separately, on its side); sac is
# the FIRST producer — until a consumer is registered the emit degrades
# to a logged line. Generic, sac-namespaced group (matches the
# ``scitex_dev.*`` entry-point convention in this repo's pyproject).
HOOKS_ENTRY_POINT_GROUP = "scitex_agent_container.hooks"

# DEPRECATED — dual-supported for transition; remove once scitex-todo
# migrates to ``scitex_agent_container.hooks``. This is the ORIGINAL group
# name (a cross-package contract): scitex-todo registers its consumer on
# it and emits card-events on it. sac loads/emits on BOTH groups so
# neither side breaks mid-migration.
LEGACY_HOOKS_ENTRY_POINT_GROUP = "scitex_todo.hooks"

# Both groups sac loads consumers from (emit reaches all) and — mirrored in
# pyproject — registers its own consumer under (so producers on either
# group reach sac). New group first so it wins any ordering.
HOOKS_ENTRY_POINT_GROUPS = (HOOKS_ENTRY_POINT_GROUP, LEGACY_HOOKS_ENTRY_POINT_GROUP)


# --- IO resolvers (run OFF the event loop via run_blocking_or) --------------


def _default_tasks_path() -> Path:
    from .._lifecycle._ci_owner import _default_tasks_path as _p

    return _p()


def _load_tasks_doc(tasks_path: Path) -> dict:
    """Read + parse ``tasks.yaml``. Fail-soft: unreadable ⇒ ``{}``."""
    if not tasks_path.is_file():
        return {}
    import yaml

    try:
        doc = yaml.safe_load(tasks_path.read_text()) or {}
    except Exception:  # stx-allow: fallback (unreadable tasks store → no cards this tick)
        return {}
    return doc if isinstance(doc, dict) else {}


def _session_last_active_ts(name: str) -> float | None:
    """Epoch seconds of ``name``'s session.jsonl last-record, or ``None``.

    Reads the last non-empty JSONL line carrying a ``ts``/``timestamp``
    via the same parser the SSE tail uses. Fail-soft: a missing /
    unreadable / tsless session yields ``None`` (unknown)."""
    from ._tail import _record_ts, _runtime_session_jsonl

    path = _runtime_session_jsonl(name)
    if not path.is_file():
        return None
    import json as _json

    last_ts: float | None = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                dt = _record_ts(record)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    last_ts = dt.timestamp()
    except OSError:  # stx-allow: fallback (unreadable session → unknown activity)
        return None
    return last_ts


def _live_agent_pids() -> dict[str, int]:
    """Map active-agent name → recorded pid, from the instances registry.

    Reuses the existing ``list_active_instances`` reader (``ended_at IS
    NULL`` rows) rather than reinventing a liveness store. Fail-soft: any
    registry hiccup ⇒ ``{}`` (nobody-live this tick — the SAFE direction:
    a card resolves to ``owner-not-live`` only once it is ALSO stale, so a
    transient registry blip cannot spam)."""
    try:
        from .._state.state_db import list_active_instances

        rows = list_active_instances()
    except Exception:  # stx-allow: fallback (registry transient → nobody resolved this tick)
        return {}
    out: dict[str, int] = {}
    for row in rows or []:
        try:
            name = str(row.get("name", "")).strip()
            pid = row.get("pid")
            if name and isinstance(pid, int):
                out.setdefault(name, pid)  # newest row wins (DESC order)
        except Exception:  # stx-allow: fallback (one bad row contributes nothing)
            continue
    return out


def resolve_liveness(owners: Iterable[str]) -> dict[str, AgentLiveness]:
    """Resolve each owner agent → :class:`AgentLiveness` (BLOCKING — run
    off-loop). ``is_live`` is "active registry row AND pid alive"; the
    session timestamp comes from ``session.jsonl``'s last record."""
    from ._agent_exec_liveness import _pid_alive

    pids = _live_agent_pids()
    out: dict[str, AgentLiveness] = {}
    for owner in owners:
        pid = pids.get(owner)
        is_live = bool(pid is not None and _pid_alive(pid))
        last_active = _session_last_active_ts(owner) if is_live else None
        out[owner] = AgentLiveness(is_live=is_live, last_active_ts=last_active)
    return out


def _resolve_doc_and_liveness(tasks_path: Path) -> tuple[dict, dict[str, AgentLiveness]]:
    """One blocking unit: load the doc, then resolve liveness for exactly
    the owners of its OPEN, unblocked cards. Bundled so the loop makes a
    SINGLE off-loop call per tick."""
    doc = _load_tasks_doc(tasks_path)
    owners = open_card_owners(doc)
    liveness = resolve_liveness(owners) if owners else {}
    return doc, liveness


# --- bus emit (degrade gracefully) ------------------------------------------


def _load_hook_consumers() -> list[Callable[[dict], Any]]:
    """Load every callable registered on sac's hook event-bus groups.

    Loads from BOTH :data:`HOOKS_ENTRY_POINT_GROUP` (the generic,
    sac-owned group) AND the legacy ``scitex_todo.hooks`` group
    (DEPRECATED — dual-supported for transition; see
    :data:`LEGACY_HOOKS_ENTRY_POINT_GROUP`), so sac's emits reach a
    consumer registered on either group and no side breaks while
    scitex-todo migrates. De-dups by loaded callable identity so a
    consumer registered under both groups is delivered to only once.

    Uses the stdlib selectable ``entry_points`` API. Fail-soft per entry:
    one un-loadable entry-point contributes nothing. Returns ``[]`` when
    both groups are empty (no consumer registered yet)."""
    from importlib.metadata import entry_points

    consumers: list[Callable[[dict], Any]] = []
    seen: set[int] = set()
    for group in HOOKS_ENTRY_POINT_GROUPS:
        try:
            eps = entry_points(group=group)
        except Exception:  # stx-allow: fallback (metadata read hiccup → no consumers this group)
            continue
        for ep in eps:
            try:
                fn = ep.load()
            except Exception as exc:  # stx-allow: fallback (one bad entry-point contributes nothing)
                logger.warning(
                    "liveness_tick: failed to load %s consumer %r: %s",
                    group,
                    getattr(ep, "name", ep),
                    exc,
                )
                continue
            if callable(fn) and id(fn) not in seen:
                seen.add(id(fn))
                consumers.append(fn)
    return consumers


def emit_anomaly(event: dict, consumers: Iterable[Callable[[dict], Any]]) -> int:
    """Deliver ``event`` to each consumer DEFENSIVELY. Returns the count
    that accepted it.

    A consumer that raises is logged and skipped — it can NEVER crash the
    loop. An empty ``consumers`` iterable delivers to nobody and returns 0
    (the caller logs the once-at-startup "no consumer" line)."""
    delivered = 0
    for fn in consumers:
        try:
            fn(event)
            delivered += 1
        except Exception as exc:  # stx-allow: fallback (a consumer failure must never crash the alarm loop)
            logger.warning(
                "liveness_tick: hook-bus consumer %r raised on emit "
                "(%s); continuing — the alarm loop must stay up",
                getattr(fn, "__name__", fn),
                exc,
            )
    return delivered


def _build_event(stuck: StuckCard, now: float) -> dict:
    return {
        "agent": stuck.agent,
        "card_id": stuck.card_id,
        "reason": stuck.reason,
        "severity": stuck.severity,
        "ts": now,
    }


async def liveness_tick_reconciler_loop(
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    stale_s: float = DEFAULT_STALE_S,
    renotify_s: float = DEFAULT_RENOTIFY_S,
    tasks_path: Path | None = None,
    tasks_doc_source: dict | None = None,
    liveness_source: dict[str, AgentLiveness] | None = None,
    consumers_source: "Iterable[Callable[[dict], Any]] | None" = None,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Long-running task launched by the ``sac listen`` lifespan.

    Each tick: resolve ``tasks.yaml`` + owner liveness OFF the event loop
    (bind-safe), run the pure :func:`find_stuck_cards` rule, and emit one
    anomaly event per newly-stuck ``(agent, card_id)`` onto sac's
    ``scitex_agent_container.hooks`` event bus (dual-supported on the
    legacy ``scitex_todo.hooks`` group during the migration). DEDUP: at
    most one emit per ``(agent,
    card_id)`` per ``renotify_s`` cooldown (in-memory; the daemon is
    long-running), so a persistently-stuck card does NOT spam per tick.

    Injection seams (tests): ``tasks_doc_source`` / ``liveness_source``
    bypass the off-loop FS/registry reads; ``consumers_source`` supplies a
    real local consumer list instead of the entry-point lookup. Production
    leaves them ``None``.

    Failure modes mirror ``periodic_drive_loop``: a tick body that raises
    is logged + retried (the loop must not die); cancellation is honoured
    at the sleep boundary and re-raised cleanly.
    """
    tasks_path = tasks_path if tasks_path is not None else _default_tasks_path()
    logger.info(
        "liveness_tick: starting (interval_s=%.1f stale_s=%.1f renotify_s=%.1f)",
        interval_s,
        stale_s,
        renotify_s,
    )

    # Startup probe: warn ONCE if no consumer is registered yet on EITHER
    # group (sac is the first producer; a card-store consumer registers
    # separately, on the legacy group today).
    if consumers_source is None and not _load_hook_consumers():
        logger.warning(
            "liveness_tick: no consumer registered on %s — anomalies will "
            "be DETECTED and logged but not delivered until a card-store "
            "consumer registers (sac is the first producer on this bus)",
            " / ".join(HOOKS_ENTRY_POINT_GROUPS),
        )

    last_emit_at: dict[tuple[str, str], float] = {}
    try:
        while True:
            try:
                if tasks_doc_source is not None or liveness_source is not None:
                    doc = tasks_doc_source if tasks_doc_source is not None else {}
                    liveness = liveness_source if liveness_source is not None else {}
                else:
                    # DEFENSE IN DEPTH (cards sac-listen-self-peer-persist-
                    # blocks-bind / sac-listen-watchdog-autorestart-alarm):
                    # the doc read + session.jsonl reads + registry read are
                    # blocking FS/SQLite. Run them OFF the event loop with a
                    # hard timeout so a slow/locked read can never starve
                    # uvicorn's bind or the running listen server.
                    from .._lifecycle._off_loop import run_blocking_or

                    doc, liveness = await run_blocking_or(
                        _resolve_doc_and_liveness,
                        tasks_path,
                        default=({}, {}),
                        op="liveness_tick resolve (tasks.yaml + session/registry FS)",
                        timeout_s=max(interval_s, 15.0),
                    )

                now = now_fn()
                stuck = find_stuck_cards(doc, liveness, now=now, stale_s=stale_s)

                if consumers_source is not None:
                    consumers = list(consumers_source)
                else:
                    consumers = _load_hook_consumers()

                for sc in stuck:
                    key = (sc.agent, sc.card_id)
                    prev = last_emit_at.get(key)
                    if prev is not None and (now - prev) < renotify_s:
                        continue  # within re-notify cooldown → suppress spam
                    event = _build_event(sc, now)
                    logger.warning(
                        "liveness_tick: ANOMALY agent=%s card=%s reason=%s "
                        "severity=%s stale_for=%.0fs",
                        sc.agent,
                        sc.card_id,
                        sc.reason,
                        sc.severity,
                        sc.stale_for_s,
                    )
                    emit_anomaly(event, consumers)
                    last_emit_at[key] = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must not die on a transient FS/registry error)
                logger.warning("liveness_tick: tick failed (%s); sleeping + retry", exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("liveness_tick: cancelled cleanly")
        raise


__all__ = [
    "AgentLiveness",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_RENOTIFY_S",
    "DEFAULT_STALE_S",
    "ENV_DISABLED",
    "ENV_INTERVAL_S",
    "ENV_RENOTIFY_S",
    "ENV_STALE_S",
    "HOOKS_ENTRY_POINT_GROUP",
    "HOOKS_ENTRY_POINT_GROUPS",
    "LEGACY_HOOKS_ENTRY_POINT_GROUP",
    "StuckCard",
    "emit_anomaly",
    "find_stuck_cards",
    "liveness_tick_reconciler_loop",
    "resolve_liveness",
]
