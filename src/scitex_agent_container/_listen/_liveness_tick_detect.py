"""Pure detection core for the liveness-tick reconciler (no IO, no asyncio).

Card ``sac-card-anchored-stop-reconciler``. Split out of
``_liveness_tick`` (the loop/IO glue) so the reconcile RULE is
unit-testable against a real in-memory tasks doc + real liveness inputs,
mirroring how ``_periodic_drive`` (pure core) is separate from
``_periodic_drive_loop`` (loop glue).

The rule keeps false positives low: a card fires an anomaly ONLY when it
is OPEN, has an assignee, declares NO blocker, and its ``last_activity``
is older than ``stale_s`` — and then ``owner-not-live`` if the owner
agent is not live, else ``owner-idle`` if the owner is live but its
session.jsonl last-record is also older than ``stale_s``; otherwise the
owner is progressing and nothing fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# A card whose ``status`` is one of these is NOT open (terminal/resolved
# or an intentionally-parked state that already declares why it isn't
# progressing). ``blocked`` / ``deferred`` / ``coordinating`` are treated
# as "carries a declared blocker" — the card is parked on purpose, so it
# must not alarm even with an empty ``blocker`` field.
TERMINAL_STATUSES = frozenset(
    {
        "done",
        "closed",
        "cancelled",
        "canceled",
        "archived",
        "wontfix",
        "resolved",
        "complete",
        "completed",
    }
)
PARKED_STATUSES = frozenset({"blocked", "deferred", "coordinating"})

# severity ladder: scale with how far past ``stale_s`` the card drifted.
_SEVERITY_CRITICAL_MULT = 4.0


@dataclass(frozen=True)
class StuckCard:
    """A detected anomaly: an OPEN, unblocked, stale card whose owner is
    not progressing. :func:`find_stuck_cards` returns these; the loop
    turns each into a ``scitex_agent_container.hooks`` event-bus event."""

    agent: str
    card_id: str
    reason: str  # "owner-not-live" | "owner-idle"
    severity: str  # "warning" | "critical"
    stale_for_s: float


@dataclass(frozen=True)
class AgentLiveness:
    """Resolved liveness for one owner agent — the input the rule reads.

    ``is_live`` — owner process is alive (registry row + ``_pid_alive``).
    ``last_active_ts`` — epoch seconds of the owner's session.jsonl
    last-record, or ``None`` when unknown (no session yet)."""

    is_live: bool
    last_active_ts: float | None


def _parse_iso_ts(value: Any) -> float | None:
    """Parse an ISO-8601 ``last_activity`` (``...Z`` or offset) to epoch s.

    Returns ``None`` for missing/unparseable values so the rule can treat
    "no known activity" distinctly from "recently active"."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_open(status: Any) -> bool:
    """True iff a card's ``status`` is neither terminal nor parked."""
    s = str(status or "").strip().lower()
    return bool(s) and s not in TERMINAL_STATUSES and s not in PARKED_STATUSES


def _has_blocker(task: dict) -> bool:
    """True iff the card declares a blocker (top-level ``blocker`` field).

    The card author has stated WHY it isn't progressing, so it must not
    alarm. A parked ``status`` is handled separately by :func:`_is_open`."""
    blocker = task.get("blocker")
    return isinstance(blocker, str) and bool(blocker.strip())


def _severity_for(stale_for_s: float, stale_s: float) -> str:
    """warning until ``_SEVERITY_CRITICAL_MULT × stale_s``, then critical."""
    if stale_s > 0 and stale_for_s >= stale_s * _SEVERITY_CRITICAL_MULT:
        return "critical"
    return "warning"


def open_card_owners(tasks_doc: dict) -> set[str]:
    """Owner agent names of OPEN, unblocked cards — the set whose liveness
    the loop needs to resolve. Pure over ``tasks_doc``."""
    owners: set[str] = set()
    for task in tasks_doc.get("tasks", []) or []:
        if not isinstance(task, dict) or not _is_open(task.get("status")):
            continue
        if _has_blocker(task):
            continue
        owner = task.get("assignee") or task.get("agent")
        if isinstance(owner, str) and owner.strip():
            owners.add(owner.strip())
    return owners


def find_stuck_cards(
    tasks_doc: dict,
    liveness: dict[str, AgentLiveness],
    now: float,
    stale_s: float,
) -> list[StuckCard]:
    """Pure reconcile rule — NO IO. Return the OPEN cards that are stuck.

    ``tasks_doc`` is a parsed ``tasks.yaml`` (``{"tasks": [...]}``).
    ``liveness`` maps an owner agent name → its resolved
    :class:`AgentLiveness`. ``now`` is epoch seconds; ``stale_s`` is the
    staleness threshold.

    A card contributes an anomaly ONLY when ALL hold:
      * its ``status`` is OPEN (not terminal, not a parked state);
      * it has an ``assignee``/``agent`` owner;
      * it declares NO ``blocker``;
      * its ``last_activity`` is older than ``stale_s`` (or absent).
    Then the reason is ``"owner-not-live"`` if the owner is not live, else
    ``"owner-idle"`` if the owner is live but its session.jsonl
    last-record is older than ``stale_s``; otherwise the owner is making
    progress and no anomaly is emitted.
    """
    out: list[StuckCard] = []
    for task in tasks_doc.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        if not _is_open(task.get("status")):
            continue
        owner = task.get("assignee") or task.get("agent")
        if not isinstance(owner, str) or not owner.strip():
            continue
        owner = owner.strip()
        if _has_blocker(task):
            continue

        card_last = _parse_iso_ts(task.get("last_activity"))
        card_stale_for = (now - card_last) if card_last is not None else float("inf")
        if card_stale_for < stale_s:
            continue  # card itself moved recently → progressing

        card_id = str(task.get("id", "")).strip()
        if not card_id:
            continue

        live = liveness.get(owner)
        if live is None or not live.is_live:
            out.append(
                StuckCard(
                    agent=owner,
                    card_id=card_id,
                    reason="owner-not-live",
                    severity=_severity_for(card_stale_for, stale_s),
                    stale_for_s=card_stale_for,
                )
            )
            continue

        # Owner IS live — anomaly only if its session is ALSO idle past
        # the threshold (hung). A recent session record ⇒ progressing.
        sess_last = live.last_active_ts
        sess_idle_for = (now - sess_last) if sess_last is not None else float("inf")
        if sess_idle_for >= stale_s:
            out.append(
                StuckCard(
                    agent=owner,
                    card_id=card_id,
                    reason="owner-idle",
                    severity=_severity_for(min(card_stale_for, sess_idle_for), stale_s),
                    stale_for_s=card_stale_for,
                )
            )
    return out


__all__ = [
    "AgentLiveness",
    "PARKED_STATUSES",
    "StuckCard",
    "TERMINAL_STATUSES",
    "find_stuck_cards",
    "open_card_owners",
]
