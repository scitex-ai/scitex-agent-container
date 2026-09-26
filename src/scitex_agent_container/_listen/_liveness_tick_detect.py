"""Pure detection core for the liveness-tick reconciler (no IO, no asyncio).

Card ``sac-card-anchored-stop-reconciler``. Split out of
``_liveness_tick`` (the loop/IO glue) so the reconcile RULE is
unit-testable against a real in-memory tasks doc + real liveness inputs,
mirroring how ``_periodic_drive`` (pure core) is separate from
``_periodic_drive_loop`` (loop glue).

The rule keeps false positives low: a card fires an anomaly ONLY when it
is OPEN, has an assignee, declares NO blocker, and its ``last_activity``
is older than ``stale_s`` — and even then, only when the owner's OWN
records corroborate that it is not progressing.

ABSENCE OF EVIDENCE IS NOT EVIDENCE OF DEATH
--------------------------------------------
The rule used to gate liveness entirely on the instances registry: an
owner with no live registry pid was declared ``owner-not-live``. The
registry turned out to be an unreliable gate — on the live fleet EVERY
active instances row carries ``pid = NULL``, so no owner could ever
resolve live, and so EVERY stale card alarmed ``owner-not-live`` /
``critical`` against agents that were provably alive (serving HTTP in the
same log). ~100 false criticals per sweep, every sweep.

So the rule is now built on POSITIVE evidence in both directions, and it
distinguishes THREE states, not two:

* **progressing** — a fresh ACTIVITY record (``last_active_ts``). Nothing
  fires. This outranks the registry entirely: an agent that is writing
  session/heartbeat records right now is alive, whatever the registry says.
* **alive but not progressing** — a fresh PROCESS record (``last_beat_ts``,
  or a live registry pid) with a stale activity record ⇒ ``owner-idle``.
* **dead** — a channel that WOULD have shown life (a recorded pid, or a
  heartbeat file) shows none ⇒ ``owner-not-live``. Positive evidence.
* **UNKNOWN** (``known=False``) — no channel that could have shown life at
  all. Emits NOTHING, and never a ``critical``. We do not know, and
  guessing "dead" is exactly what produced the flood.
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
    turns each into a ``scitex_cards.hooks`` bus event."""

    agent: str
    card_id: str
    reason: str  # "owner-not-live" | "owner-idle"
    severity: str  # "warning" | "critical"
    stale_for_s: float


@dataclass(frozen=True)
class AgentLiveness:
    """Resolved liveness for one owner agent — the input the rule reads.

    FOUR fields, because "alive", "dead" and "we cannot tell" are three
    different answers and the old two-field shape could only express two
    of them. Collapsing UNKNOWN into "dead" is what flooded the daemon log
    with false criticals (see the module docstring).

    ``is_live``
        POSITIVE registry evidence — an active instances row whose recorded
        pid is alive. ``False`` means "the registry did not prove it live",
        NOT "it is dead": on the live fleet the registry records
        ``pid = NULL`` on every row, so this signal is routinely absent for
        a perfectly healthy agent. Corroborating only — never a gate.
    ``last_active_ts``
        Epoch seconds of the owner's newest ACTIVITY record (its
        session.jsonl last record, or its heartbeat's ``ts`` field —
        whichever is newer), or ``None`` when it has no such record. A
        fresh one is PROOF OF PROGRESS and outranks every other signal.
    ``known``
        Whether we had ANY channel that would have shown life had the owner
        been alive (a recorded pid, or a heartbeat file). ``False`` ⇒
        UNKNOWN: the rule stays SILENT rather than guess "dead".
    ``last_beat_ts``
        Epoch seconds of the owner's newest PROCESS record — when its
        heartbeat writer last beat. A fresh beat proves the process is
        alive even while it makes no progress, which is exactly the
        ``owner-idle`` case.

    The two defaults keep every existing 2-arg construction meaning what it
    always meant: ``AgentLiveness(is_live=False, last_active_ts=None)`` is
    still a KNOWN-dead owner.
    """

    is_live: bool
    last_active_ts: float | None
    known: bool = True
    last_beat_ts: float | None = None


# The verdict for an owner the resolver never produced an entry for. NOT
# "dead" — we simply never looked, or the lookup could not be made.
UNKNOWN_LIVENESS = AgentLiveness(
    is_live=False, last_active_ts=None, known=False, last_beat_ts=None
)


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


def _idle_for(ts: float | None, now: float) -> float:
    """Seconds since ``ts``; ``inf`` when there is no such record at all."""
    return (now - ts) if ts is not None else float("inf")


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
    *,
    fleet_last_beat_ts: float | None = None,
) -> list[StuckCard]:
    """Pure reconcile rule — NO IO. Return the OPEN cards that are stuck.

    ``tasks_doc`` is a parsed ``tasks.yaml`` (``{"tasks": [...]}``).
    ``liveness`` maps an owner agent name → its resolved
    :class:`AgentLiveness`. ``now`` is epoch seconds; ``stale_s`` is the
    staleness threshold.

    A card is a CANDIDATE only when ALL hold:
      * its ``status`` is OPEN (not terminal, not a parked state);
      * it has an ``assignee``/``agent`` owner;
      * it declares NO ``blocker``;
      * its ``last_activity`` is older than ``stale_s`` (or absent).

    A candidate then resolves against the owner's OWN records, in strict
    order of evidential strength — positive evidence first, silence when
    there is none:

      1. **Fresh activity record** ⇒ the owner is demonstrably alive AND
         progressing ⇒ NOTHING fires. This deliberately outranks the
         registry: the strongest positive signal must never be overruled
         by a missing or stale registry row.
      2. **Fresh heartbeat, or a live registry pid** ⇒ the process is alive
         but is not progressing ⇒ ``owner-idle``.
      3. **UNKNOWN** (``known=False``, or no entry at all) ⇒ we had no
         channel that could have shown life ⇒ NOTHING fires. Never a
         critical on an owner we were unable to resolve.
      4. Otherwise the owner is dead on POSITIVE evidence — a channel that
         would have shown life shows none ⇒ ``owner-not-live``.
    """
    # Is the heartbeat WRITER itself working this tick?
    #
    # The beats we read as proof-of-life come from ONE shared writer (sibling
    # loops inside ``sac listen``), which is known to blow its budget and get
    # abandoned. When it stops, EVERY agent's beat freezes at once — for the
    # same reason. That is a fact about the WRITER, not about any agent, so a
    # frozen beat only convicts a PARTICULAR owner if the writer is still
    # demonstrably beating for somebody.
    #
    # Without this, the fix would merely swap one fleet-wide false-death flood
    # (a registry that records no pids) for another (a writer that records no
    # beats) — the same inversion down a different channel.
    #
    # ``fleet_last_beat_ts`` is the newest beat ANYWHERE in the fleet, not just
    # among these owners. That distinction is load-bearing: the owners of stale
    # cards are a biased sample (skewed toward dead agents), so inferring
    # "the writer is down" from THEIR silence would let a lone dead owner
    # suppress its own alarm. ``None`` means "no fleet reading was supplied" —
    # then we trust the beats rather than invent a suppression.
    beats_trustworthy = (
        True
        if fleet_last_beat_ts is None
        else _idle_for(fleet_last_beat_ts, now) < stale_s
    )

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
        card_stale_for = _idle_for(card_last, now)
        if card_stale_for < stale_s:
            continue  # card itself moved recently → progressing

        card_id = str(task.get("id", "")).strip()
        if not card_id:
            continue

        # No resolved entry ⇒ we never determined this owner's liveness.
        # That is UNKNOWN, not dead.
        live = liveness.get(owner, UNKNOWN_LIVENESS)

        # (1) PROGRESS outranks everything. An owner writing activity
        #     records right now is alive and working — whatever the
        #     registry claims, and whether or not it claims anything at all.
        activity_idle_for = _idle_for(live.last_active_ts, now)
        if activity_idle_for < stale_s:
            continue

        # (2) The process is demonstrably alive (its heartbeat writer is
        #     still beating, or the registry recorded a pid that is alive)
        #     but nothing is moving on the card.
        beat_idle_for = _idle_for(live.last_beat_ts, now)
        if live.is_live or beat_idle_for < stale_s:
            out.append(
                StuckCard(
                    agent=owner,
                    card_id=card_id,
                    reason="owner-idle",
                    severity=_severity_for(
                        min(card_stale_for, activity_idle_for), stale_s
                    ),
                    stale_for_s=card_stale_for,
                )
            )
            continue

        # (3) UNKNOWN — nothing could have told us either way, so say
        #     nothing. Guessing "dead" here is precisely the inversion that
        #     flooded the log with false criticals.
        if not live.known:
            continue

        # (3b) UNKNOWN, second kind: this owner HAS a beat record, but it is
        #      frozen at a moment when NOBODY in the fleet is beating. The
        #      writer stopped — we cannot tell whether this agent did too.
        #      Withhold the verdict rather than indict every agent at once.
        if live.last_beat_ts is not None and not beats_trustworthy:
            continue

        # (4) POSITIVE evidence of death: a channel that would have shown
        #     life (a recorded pid / a heartbeat the writer is still able to
        #     refresh) shows none, and there is no fresh activity either.
        out.append(
            StuckCard(
                agent=owner,
                card_id=card_id,
                reason="owner-not-live",
                severity=_severity_for(card_stale_for, stale_s),
                stale_for_s=card_stale_for,
            )
        )
    return out


__all__ = [
    "AgentLiveness",
    "PARKED_STATUSES",
    "StuckCard",
    "TERMINAL_STATUSES",
    "UNKNOWN_LIVENESS",
    "find_stuck_cards",
    "open_card_owners",
]
