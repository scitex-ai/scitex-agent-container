"""Route reconcile verdicts to a SEEN surface — and prove we are alive.

Two rails, both idempotent scitex-todo cards, both SIDE rails (a board
write that fails prints loudly and can NEVER crash or skip the restart
pass that feeds it — the pass's job is resurrecting corpses; telling the
board is secondary and must not be able to take the primary down).

1. **Down cards** (``fleet-agent-down-<name>``) — one per agent this
   enforcer could NOT recover. Upsert on unrecoverable, resolve the moment
   the agent is back, exactly like :mod:`.._hostsync._alarm` does for peer
   drift. An enforcer that gives up SILENTLY is just the original bug with
   extra steps.

2. **The heartbeat card** (``fleet-reconciler-heartbeat``) — the answer to
   "who watches the watcher". The reconciler is correctly independent of
   the agents it restarts (a systemd timer, outside their failure domain),
   but that only moves the question: if its timer is never enabled, or its
   unit fails, or its command silently no-ops, the fleet goes back to dying
   invisibly and NOTHING says so — because the only thing that would have
   told us is the thing that died.

   Precedent, same failure class (scitex-hpc, 2026-07-13): a walltime trap
   FIRED on schedule, but its resubmit silently no-op'd because ``sbatch``
   had been scrubbed off PATH. ~76 CI runners died. SLURM logged a
   signal-kill, not "renewal failed", so nothing alarmed.

   The regress terminates because the alarm is delivered by a DIFFERENT
   system: scitex-todo's stale-active nudge fires on any ``in_progress``
   card untouched beyond its threshold, and that nudge rides the
   scitex-todo agent's own notifyd to a human-watched surface. A reconciler
   that stops ticking lets its own heartbeat card go stale, and the BOARD
   shouts. One system's silence becomes another system's alarm — two
   independent systems watching each other, rather than one script policing
   itself.

   Hence :func:`upsert_heartbeat` runs on EVERY pass including a dry-run,
   and above all on a pass that found nothing to do: "0 restarted, all
   healthy" is the most important tick there is, because a heartbeat that
   only appears when there is trouble cannot distinguish HEALTHY from DEAD.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ._rule import Verdict

__all__ = [
    "AlarmOutcome",
    "CARD_ID_PREFIX",
    "HEARTBEAT_CARD_ID",
    "STATE_CARD_ID",
    "card_id_for",
    "clear_state_alarm",
    "route_reports_to_cards",
    "upsert_heartbeat",
    "upsert_state_alarm",
]

#: Stable per-agent card id — idempotency is by id, so a re-run updates in
#: place instead of duplicating.
CARD_ID_PREFIX = "fleet-agent-down-"

#: The reconciler's own liveness beacon. ONE card, stable id, forever.
HEARTBEAT_CARD_ID = "fleet-reconciler-heartbeat"

#: Raised when the reconciler cannot read its OWN restart memory, so its
#: rate limits are unenforceable and it has refused to restart anything.
#: One card for the whole pass — the cause is the reconciler's, not any
#: agent's.
STATE_CARD_ID = "fleet-reconciler-state-unreadable"

#: Owner + author of the down cards. A pseudo-agent, not a human.
_AGENT = "sac.fleet-reconcile"

#: The heartbeat is owned by the PACKAGE identity rather than the
#: pseudo-agent: a human must own noticing that sac's enforcer went quiet.
_HEARTBEAT_ASSIGNEE = "scitex-agent-container"

#: A down card lands on the board's BLOCKING-YOU view — the surface the
#: operator already watches.
_STATUS = "blocked"
_BLOCKER = "operator-decision"

#: Verdicts meaning "this agent is DOWN and restarting is NOT fixing it" →
#: card. Deliberately narrow: a card must mean a human is needed, or the
#: operator learns to scroll past them.
_UNRECOVERED = (Verdict.FAILED, Verdict.OVER_BUDGET)

#: Verdicts meaning "this agent is UP" → resolve any card it had.
#:
#: Three DOWN verdicts are deliberately in NEITHER bucket — down, but not a
#: card, because each resolves itself without a human:
#:
#: * ``COOLING-DOWN`` — inside its 30min debounce. The timer ticks every
#:   5min, so a HEALTHY restart is cooling down for its next five ticks;
#:   carding that would raise a card for every successful heal.
#: * ``CAPPED`` — our own per-pass throttle, not the agent's fault. The next
#:   tick tries it. Carding would be noise; resolving would be a lie.
#: * ``UNKNOWN`` — blindness is fleet-WIDE (we are in a container, or tmux
#:   is wedged), so carding per-agent would mint ~93 cards for ONE cause.
#:
#: All three still print loudly and still drive the exit code non-zero — not
#: carding is not the same as not saying.
_RECOVERED = (Verdict.OK, Verdict.RESTARTED)


def card_id_for(name: str) -> str:
    """The stable card id for ``name``'s down alarm."""
    return f"{CARD_ID_PREFIX}{name}"


def _now_iso(now: datetime | None) -> str:
    """A timestamp in scitex-todo's OWN canonical shape, not ours.

    Second resolution and a ``Z`` suffix, byte-identical to the store's
    private ``_utc_now_iso``. This is not cosmetic on two counts, and both
    bite the heartbeat specifically:

    * ``last_activity`` is what the board's stale-active nudge reads to
      decide the reconciler has gone quiet. That nudge IS this design's
      alarm-for-a-dead-alarm, so a stamp it cannot parse would silently
      disarm the one rail that reports our own death.
    * the store's own docstring says the ``Z`` suffix (rather than
      ``+00:00``) is what makes the string "round-trip losslessly through
      YAML" — an unquoted ``+00:00`` stamp risks being re-read as a
      timestamp OBJECT rather than a string.

    Mirrored rather than imported: ``_utc_now_iso`` is private to
    ``scitex_todo._store``, and this needs the injectable ``now`` seam that
    a fixed-clock test provides.
    """
    stamp = now or datetime.now(timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AlarmOutcome:
    """What the routing did this run — so the caller is never silent."""

    carded: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def summary_line(self) -> str:
        parts = [f"{len(self.carded)} down card(s)", f"{len(self.cleared)} cleared"]
        if self.failed:
            parts.append(f"{len(self.failed)} DELIVERY-FAILED")
        return "scitex-todo alarm: " + ", ".join(parts)


def _card_exists(store: str | None, card_id: str) -> bool:
    """True when ``card_id`` is already on the board.

    A MISSING store file (nothing carded yet) and a missing id BOTH mean
    "no such card" — the first run against a fresh board raises
    ``FileNotFoundError``, later runs raise ``TaskNotFoundError``.
    """
    from scitex_todo import TaskNotFoundError, get_task

    # stx-allow: fallback (reason: "does this card exist yet?" — a missing store file or missing id is a normal False, not an error; any other load failure propagates to the caller's per-card guard, which reports it loudly)
    try:
        get_task(store, card_id)
        return True
    except (TaskNotFoundError, FileNotFoundError):
        return False


def _down_card(name: str, verdict: Verdict, detail: str) -> tuple[str, str]:
    """Title + note for an agent this enforcer could not bring back."""
    title = f"[fleet] {name} is DOWN and sac could NOT recover it ({verdict.value})"
    note = (
        f"agent: {name}\n"
        f"verdict: {verdict.value}\n"
        f"why: {detail}\n\n"
        "sac's reconciler found this agent's tmux session GONE and its spec "
        "asking to be kept running, but it did not restart it — the reason is "
        "above. A restart LOOP is worse than a down agent, so the enforcer "
        "stops and asks instead of trying harder.\n\n"
        "Investigate:\n"
        f"    sac agents list | grep {name}\n"
        f"    sac agents tail {name}\n"
        "Restart by hand once the cause is understood:\n"
        f"    sac agents restart {name} --yes\n"
        "Preview what the reconciler would do (mutates nothing):\n"
        "    sac agents reconcile"
    )
    return title, note


def _upsert(
    store: str | None,
    card_id: str,
    title: str,
    note: str,
    now: datetime | None,
    *,
    status: str = _STATUS,
    blocker: str | None = _BLOCKER,
    assignee: str = _AGENT,
) -> None:
    """Create the card, or update it in place — never duplicate.

    Re-asserting ``status`` also REOPENS a card a previous clean run
    resolved: an agent that dies, is fixed (card resolved), then dies again
    must alarm again.
    """
    from scitex_todo import add_task, update_task

    if _card_exists(store, card_id):
        kwargs: dict[str, Any] = {
            "title": title,
            "note": note,
            "status": status,
            "last_activity": _now_iso(now),
        }
        if blocker is not None:
            kwargs["blocker"] = blocker
        update_task(store, card_id, **kwargs)
    else:
        kwargs = {
            "id": card_id,
            "title": title,
            "status": status,
            "note": note,
            "scope": f"agent:{assignee}",
            "assignee": assignee,
            "created_by": _AGENT,
        }
        if blocker is not None:
            kwargs["blocker"] = blocker
        add_task(store, **kwargs)


def _clear(store: str | None, card_id: str) -> bool:
    """Resolve the agent's card if present + still open. Returns did-clear.

    Idempotent: no card, or an already-resolved card, is a quiet no-op — a
    healthy agent that was never down must not create then resolve a
    phantom card.
    """
    from scitex_todo import get_task, resolve_task

    if not _card_exists(store, card_id):
        return False
    if get_task(store, card_id).get("status") == "done":
        return False
    resolve_task(store, card_id, actor=_AGENT)
    return True


def route_reports_to_cards(
    reports: Iterable[Any],
    *,
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> AlarmOutcome:
    """Upsert / resolve one card per agent from this pass's ``reports``.

    Never raises: a per-agent card-write failure is printed loudly to
    ``err_stream`` and recorded in :attr:`AlarmOutcome.failed`, so one
    unwritable board never suppresses the rest of the fleet's alarms — and
    never unwinds a restart that already happened.

    ``reports`` are :class:`._pass.AgentReport` objects (duck-typed here to
    keep this module importable without :mod:`._pass`).
    """
    stream = err_stream if err_stream is not None else sys.stderr
    carded: list[str] = []
    cleared: list[str] = []
    failed: list[str] = []

    for report in reports:
        name = report.name
        card_id = card_id_for(name)
        # stx-allow: fallback (reason: card delivery is a SIDE rail — one agent's board-write failure must never crash the reconcile pass that feeds it, nor suppress the other agents' alarms; it is reported loudly on stderr and recorded in AlarmOutcome.failed)
        try:
            if report.verdict in _UNRECOVERED:
                _upsert(
                    store,
                    card_id,
                    *_down_card(name, report.verdict, report.detail),
                    now=now,
                )
                carded.append(name)
            elif report.verdict in _RECOVERED and _clear(store, card_id):
                cleared.append(name)
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            failed.append(name)
            print(
                f"[fleet-reconcile-alarm] {name}: scitex-todo card delivery "
                f"FAILED — {exc} (the reconcile verdict above is UNCHANGED and "
                f"any restart it performed still happened)",
                file=stream,
            )

    return AlarmOutcome(
        carded=tuple(carded), cleared=tuple(cleared), failed=tuple(failed)
    )


def upsert_state_alarm(
    detail: str,
    *,
    path: Any = "",
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> bool:
    """Shout that the reconciler cannot read its OWN state. Returns did-write.

    The reconciler exists to catch silent death; it must not die silently
    itself. When its restart history is denied or corrupt the rate limits
    are unenforceable, so it REFUSES to restart — and a refusal nobody hears
    is a no-op, which is exactly the "renewal mechanism that cannot report
    its own failure" class this whole design is built against.

    Measured precedent (Spartan, 2026-07-16): ``~/.scitex`` is a SYMLINK
    into a project whose membership was revoked, so every ``$HOME``-resolved
    path under it became permission-denied for fresh processes — while
    everything still LOOKED installed and configured.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    title = "[fleet] sac reconciler CANNOT READ ITS OWN STATE — restarts halted"
    note = (
        f"state file: {path or '?'}\n"
        f"problem: {detail}\n\n"
        "sac's reconciler keeps ONE piece of state: the history of which "
        "agents it has already auto-restarted. That file is the ONLY thing "
        "enforcing the debounce (30min/agent) and the hourly cap (2/agent). "
        "It could not be read, so those limits cannot be enforced — and an "
        "unenforceable budget is not a budget. Restarting anyway would make "
        "EVERY corpse restartable on EVERY 5-minute tick, forever.\n\n"
        "So the reconciler has REFUSED to restart anything. Dead agents are "
        "currently staying dead. This needs a human.\n\n"
        "Most likely causes:\n"
        "  * the state root ($SCITEX_DIR, default ~/.scitex) is unreadable — "
        "on some hosts it is a SYMLINK into a project whose membership can "
        "be revoked, which denies every path under it at once;\n"
        "  * the file is corrupt (delete it to reset the budget DELIBERATELY).\n\n"
        "Diagnose + pin the state somewhere durable:\n"
        f"    ls -l {path or '<state file>'}\n"
        "    sac agents reconcile            # dry-run; names what it sees\n"
        "    export SAC_RECONCILE_HISTORY=/var/tmp/sac-fleet-reconcile.json"
    )
    # stx-allow: fallback (reason: this alarm is the LAST rail — if the board is unreachable too, the loud stderr line and the non-zero exit code must still carry the failure rather than raising into the pass)
    try:
        _upsert(store, STATE_CARD_ID, title, note, now)
        return True
    except Exception as exc:
        print(
            f"[fleet-reconcile-alarm] STATE-UNREADABLE card delivery FAILED "
            f"— {exc}. The reconciler cannot read its own restart history "
            f"({detail}) AND cannot tell the board. Restarts are halted.",
            file=stream,
        )
        return False


def clear_state_alarm(*, store: str | None = None, err_stream: Any = None) -> bool:
    """Resolve the state alarm once we can read our own memory again."""
    stream = err_stream if err_stream is not None else sys.stderr
    # stx-allow: fallback (reason: side rail — failing to CLEAR a stale alarm must never crash a pass that is otherwise working correctly)
    try:
        return _clear(store, STATE_CARD_ID)
    except Exception as exc:
        print(
            f"[fleet-reconcile-alarm] could not resolve {STATE_CARD_ID} — {exc}",
            file=stream,
        )
        return False


def _heartbeat_note(
    stats: Mapping[str, int], *, mode: str, host: str, now: datetime | None
) -> str:
    counted = "\n".join(f"  {k:<22} {v}" for k, v in stats.items())
    return (
        f"last run: {_now_iso(now)}\n"
        f"mode:     {mode}\n"
        f"host:     {host}\n\n"
        f"{counted}\n\n"
        "This card is sac's fleet-reconciler LIVENESS BEACON. It is refreshed "
        "on every pass — including passes that find nothing wrong, which are "
        "the ticks that prove the mechanism is alive at all.\n\n"
        "IF THIS CARD IS STALE, THE ENFORCER IS NOT RUNNING, and agents that "
        "die are once again staying dead unnoticed. That is what the card is "
        "for: sac cannot alarm on its own silence, so the board does it "
        "instead (scitex-todo's stale-active nudge on an untouched "
        "in_progress card).\n\n"
        "Note the `mode` line: a hand-run `sac agents reconcile` (dry-run) "
        "also refreshes this card. If mode is not `apply`, the scheduled "
        "timer may still be dead even though the card looks fresh.\n\n"
        "Check the timer:\n"
        "    systemctl --user list-timers | grep fleet-reconcile\n"
        "    scitex-dev ecosystem timers\n"
        "Run a pass by hand (mutates nothing):\n"
        "    sac agents reconcile"
    )


def upsert_heartbeat(
    stats: Mapping[str, int],
    *,
    mode: str,
    host: str = "",
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> bool:
    """Refresh the reconciler's own liveness card. Returns did-write.

    Kept ``in_progress`` (NOT ``done``) on purpose: only an OPEN card is
    watched by scitex-todo's stale-active nudge, and that nudge going off
    IS the alarm for a dead reconciler. Resolving it would silence the one
    thing that can report our death.

    A SIDE rail like the down cards: a board-write failure prints loudly
    and returns ``False``; it never raises into the pass.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    # stx-allow: fallback (reason: the heartbeat is a SIDE rail — telling the board we are alive must never crash the pass that restarts corpses; a failure here is printed loudly and reported to the caller)
    try:
        _upsert(
            store,
            HEARTBEAT_CARD_ID,
            f"[fleet] sac reconciler heartbeat — last {mode} pass {_now_iso(now)}",
            _heartbeat_note(stats, mode=mode, host=host, now=now),
            now,
            status="in_progress",
            blocker=None,
            assignee=_HEARTBEAT_ASSIGNEE,
        )
        return True
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[fleet-reconcile-alarm] HEARTBEAT card delivery FAILED — {exc}. "
            f"The reconcile pass itself was UNAFFECTED, but the board can no "
            f"longer tell anyone whether this enforcer is alive — fix the "
            f"scitex-todo store.",
            file=stream,
        )
        return False
