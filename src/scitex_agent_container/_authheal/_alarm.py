"""Route login-expired-restart verdicts to a SEEN surface — and prove liveness.

Two idempotent scitex-todo rails, both SIDE rails (a board write that fails
prints loudly and can NEVER crash or skip the restart pass that feeds it):

1. **Escalation cards** (``fleet-agent-login-expired-<name>``) — one per agent
   that is STILL login-expired after the hourly restart cap. Restarting is not
   fixing it, so instead of an infinite bounce the agent is handed to a human.
   Upsert on OVER-BUDGET/FAILED; resolve the moment the agent is no longer
   login-expired (restarted, or recovered on its own and gone from the pass).

2. **Heartbeat card** (``fleet-login-expired-restarter-heartbeat``) — the answer
   to "who watches the watcher": refreshed on EVERY pass, so if this timer stops
   ticking its card goes stale and scitex-todo's stale-active nudge shouts. One
   system's silence becomes another's alarm — the same design as
   :mod:`.._reconcile._alarm`.

Mirrors the reconcile alarm's shape but talks to the scitex-todo PUBLIC API
directly (``add_task``/``update_task``/``get_task``/``resolve_task``/
``list_tasks``) so this package stays self-contained and its card wording is
specific to the login-expired case.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .._reconcile._rule import Verdict

__all__ = [
    "AlarmOutcome",
    "CARD_ID_PREFIX",
    "HEARTBEAT_CARD_ID",
    "card_id_for",
    "route_reports_to_cards",
    "upsert_heartbeat",
]

#: Stable per-agent card id — idempotency is by id, so a re-run updates in place.
CARD_ID_PREFIX = "fleet-agent-login-expired-"

#: The restarter's own liveness beacon. ONE card, stable id, forever.
HEARTBEAT_CARD_ID = "fleet-login-expired-restarter-heartbeat"

#: Owner + author of the escalation cards (a pseudo-agent, not a human).
_AGENT = "sac.restart-login-expired-agents"

#: The heartbeat is owned by the PACKAGE identity: a human must own noticing
#: that this restarter went quiet.
_HEARTBEAT_ASSIGNEE = "scitex-agent-container"

_STATUS = "blocked"
_BLOCKER = "operator-decision"

#: Verdicts meaning "still login-expired and a restart is NOT fixing it" → card.
_UNRECOVERED = (Verdict.OVER_BUDGET, Verdict.FAILED)

#: Verdicts meaning "we acted / it is on the mend" → resolve any card it had.
_CLEARED_BY = (Verdict.RESTARTED,)


def card_id_for(name: str) -> str:
    """The stable escalation-card id for ``name``."""
    return f"{CARD_ID_PREFIX}{name}"


def _now_iso(now: datetime | None) -> str:
    """A timestamp in scitex-todo's canonical shape (second-res, ``Z`` suffix)."""
    stamp = now or datetime.now(timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AlarmOutcome:
    """What the routing did this run — so the caller is never silent."""

    carded: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def _card_exists(store: str | None, card_id: str) -> bool:
    from scitex_todo import TaskNotFoundError, get_task

    # stx-allow: fallback (reason: "does this card exist yet?" — a missing store file or missing id is a normal False; any other load failure propagates to the caller's per-card guard, which reports it loudly)
    try:
        get_task(store, card_id)
        return True
    except (TaskNotFoundError, FileNotFoundError):
        return False


def _escalation_card(name: str, verdict: Verdict, detail: str) -> tuple[str, str]:
    """Title + note for an agent this restarter could not heal."""
    title = (
        f"[fleet] {name} is STILL login-expired after auto-restart ({verdict.value})"
    )
    note = (
        f"agent: {name}\n"
        f"verdict: {verdict.value}\n"
        f"why: {detail}\n\n"
        "sac's login-expired restarter found this LIVE agent wedged behind a "
        "frozen auth banner and auto-restarted it, but it is STILL login-expired "
        "after the hourly cap. A restart LOOP is worse than a wedged agent, so "
        "the restarter stops and asks a human instead of bouncing it forever.\n\n"
        "The usual cause is a genuine account problem (the shared OAuth account "
        "cannot refresh), which no restart fixes.\n\n"
        "Investigate:\n"
        f"    sac agents auth-status | grep {name}\n"
        f"    sac agents tail {name}\n"
        "Restart by hand once the account is healthy:\n"
        f"    sac agents restart {name} --yes"
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

    Re-asserting ``status`` also REOPENS a card a previous clean run resolved:
    an agent that wedges, is healed (card resolved), then wedges again must
    alarm again.
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
    """Resolve the card if present + still open. Returns did-clear (idempotent)."""
    from scitex_todo import get_task, resolve_task

    if not _card_exists(store, card_id):
        return False
    if get_task(store, card_id).get("status") == "done":
        return False
    resolve_task(store, card_id, actor=_AGENT)
    return True


def _carded_agents(store: str | None) -> list[str]:
    """Names of every agent that currently has an OPEN escalation card.

    Lets a pass RESOLVE the card of an agent that recovered on its own — it is
    simply absent from this pass's reports, so without this it would leave a
    stale alarm on the board forever.
    """
    from scitex_todo import list_tasks

    # stx-allow: fallback (reason: card-clearing is a SIDE rail — a store read failure must never crash the restart pass; an empty list just means "clear nothing this pass")
    try:
        rows = list_tasks(store, id_prefix=CARD_ID_PREFIX)
    except Exception:
        return []
    return [
        str(r["id"])[len(CARD_ID_PREFIX) :]
        for r in rows
        if str(r.get("id", "")).startswith(CARD_ID_PREFIX) and r.get("status") != "done"
    ]


def route_reports_to_cards(
    reports: Iterable[Any],
    *,
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> AlarmOutcome:
    """Upsert / resolve one escalation card per agent from this pass's reports.

    Never raises: a per-agent card-write failure is printed loudly and recorded
    in :attr:`AlarmOutcome.failed`, so one unwritable board never suppresses the
    rest of the fleet's alarms — nor unwinds a restart that already happened.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    reports = list(reports)
    active = {r.name for r in reports}
    carded: list[str] = []
    cleared: list[str] = []
    failed: list[str] = []

    for report in reports:
        name = report.name
        card_id = card_id_for(name)
        # stx-allow: fallback (reason: card delivery is a SIDE rail — one agent's board-write failure must never crash the pass that feeds it, nor suppress the other agents' alarms; it is reported loudly and recorded in AlarmOutcome.failed)
        try:
            if report.verdict in _UNRECOVERED:
                _upsert(
                    store,
                    card_id,
                    *_escalation_card(name, report.verdict, report.detail),
                    now=now,
                )
                carded.append(name)
            elif report.verdict in _CLEARED_BY and _clear(store, card_id):
                cleared.append(name)
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            failed.append(name)
            print(
                f"[login-expired-restart-alarm] {name}: scitex-todo card "
                f"delivery FAILED — {exc} (the verdict above is UNCHANGED and any "
                f"restart it performed still happened)",
                file=stream,
            )

    # Recovered-on-their-own: an agent with an open card that is NOT in this
    # pass's reports is no longer login-expired → resolve its stale card.
    #
    # An UNOBSERVED agent IS in the reports, so its card survives this sweep.
    # That is the point: resolving an escalation card on a reading we never
    # took would be a false all-clear in its most durable form — the board
    # would say a human had nothing left to look at, on the strength of the
    # pass having failed to look.
    for name in _carded_agents(store):
        if name in active:
            continue
        # stx-allow: fallback (reason: side rail — a failed clear must never crash the pass)
        try:
            if _clear(store, card_id_for(name)):
                cleared.append(name)
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            failed.append(name)
            print(
                f"[login-expired-restart-alarm] {name}: could not resolve stale "
                f"card — {exc}",
                file=stream,
            )

    return AlarmOutcome(
        carded=tuple(carded), cleared=tuple(cleared), failed=tuple(failed)
    )


def _heartbeat_note(
    stats: Mapping[str, int], *, mode: str, host: str, now: datetime | None
) -> str:
    counted = "\n".join(f"  {k:<22} {v}" for k, v in stats.items())
    return (
        f"last run: {_now_iso(now)}\n"
        f"mode:     {mode}\n"
        f"host:     {host}\n\n"
        f"{counted}\n\n"
        "This card is sac's login-expired-restarter LIVENESS BEACON, refreshed "
        "on every pass — including passes that find nothing wrong, which are the "
        "ticks that prove the mechanism is alive at all.\n\n"
        "IF THIS CARD IS STALE, THE RESTARTER IS NOT RUNNING, and live agents "
        "wedged behind an auth banner are once again staying wedged unnoticed.\n\n"
        "Check the timer:\n"
        "    systemctl --user list-timers | grep restart-login-expired\n"
        "Run a pass by hand (read-only):\n"
        "    sac agents restart-login-expired --check"
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
    """Refresh the restarter's own liveness card. Returns did-write.

    Kept ``in_progress`` (NOT ``done``): only an OPEN card is watched by
    scitex-todo's stale-active nudge, and that nudge firing IS the alarm for a
    dead restarter. A SIDE rail — a board-write failure prints loudly and returns
    ``False``; it never raises into the pass.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    # stx-allow: fallback (reason: the heartbeat is a SIDE rail — telling the board we are alive must never crash the pass that restarts wedged agents; a failure here is printed loudly and reported to the caller)
    try:
        _upsert(
            store,
            HEARTBEAT_CARD_ID,
            f"[fleet] sac login-expired-restarter heartbeat — last {mode} pass "
            f"{_now_iso(now)}",
            _heartbeat_note(stats, mode=mode, host=host, now=now),
            now,
            status="in_progress",
            blocker=None,
            assignee=_HEARTBEAT_ASSIGNEE,
        )
        return True
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[login-expired-restart-alarm] HEARTBEAT card delivery FAILED — "
            f"{exc}. The pass itself was UNAFFECTED, but the board can no longer "
            f"tell anyone whether this restarter is alive.",
            file=stream,
        )
        return False
