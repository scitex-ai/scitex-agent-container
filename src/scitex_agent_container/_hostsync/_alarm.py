"""Route ``sac host sync --check`` drift verdicts to a SEEN surface.

Stage-0 of the one-way central-sync plan (card
``sac-one-way-central-sync-staged-plan-20260714``) shipped
``sac host sync --check`` — a read-only drift detector that mutates
nothing and exits non-zero on drift. But a detector that only sets an
exit code is an ALARM WITH NO ONE LISTENING: its shout lands in a
journald line nobody reads, which is exactly the anti-pattern the plan
warns against — "an emit is a notification, not a mechanism". That is
how a five-release-stale checkout stayed invisible until someone looked
by hand.

This module makes the shout SEEN. For each peer's read-only
:class:`~._sync.SyncResult` it upserts an IDEMPOTENT scitex-todo card on
the board's BLOCKING-YOU view (``status=blocked``,
``blocker=operator-decision`` — the surface the operator already
monitors), and RESOLVES that card the moment the peer goes clean again so
a fixed drift stops shouting.

It is a REPORT, never an enforcer. It mutates nothing on any peer and
never triggers a sync — that (Stage 1) is deliberately out of scope. The
only writes it makes are to the shared task board.

Three-state honest (the rule sac has paid for, twice):

* **DRIFTED** (behind / ahead / diverged / dirty) → a card NAMING the
  drift (which peer, and how it differs).
* **UNDETERMINED** (unreachable / no-module / not-a-checkout) → a card
  too, but clearly labelled UNKNOWN, never rendered as clean. "I could
  not look" must never read as "I looked and it was fine".
* **CURRENT / SYNCED** (clean) → resolve the peer's card.

It reuses the existing scitex-todo integration — the same public writer
sac's account-refresh alarm rail uses (:mod:`.._account.refresh_alarm`) —
rather than hand-rolling a todo client. Like that rail, delivery is a
SIDE rail: a per-card failure is printed loudly to stderr and never
crashes the check that feeds it. And, like a missing scitex-todo install,
the whole thing degrades to "printed loudly, nothing recorded" rather
than taking the check down.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ._model import PeerSyncReport
from ._sync import SyncResult

__all__ = ["AlarmOutcome", "CARD_ID_PREFIX", "card_id_for", "route_reports_to_cards"]

#: Stable per-peer card id, so a re-run updates in place instead of
#: duplicating (idempotency is by id).
CARD_ID_PREFIX = "host-sync-drift-"

#: The card's owner + author. A pseudo-agent, not a human: the card is a
#: standing "this peer is not running the centre's code" fact that the
#: operator resolves, mirroring the accounts-refresh alarm's convention.
_AGENT = "sac.host-sync-check"

#: A determined-but-drifted card and an undetermined card BOTH land on the
#: board's BLOCKING-YOU view (``status=blocked`` AND
#: ``blocker=operator-decision``) — the seen surface the operator already
#: watches. The card TEXT is what distinguishes drift from unknown.
_STATUS = "blocked"
_BLOCKER = "operator-decision"


def card_id_for(peer: str) -> str:
    """The stable card id for ``peer``'s drift alarm."""
    return f"{CARD_ID_PREFIX}{peer}"


@dataclass(frozen=True)
class AlarmOutcome:
    """What the routing did this run — so the caller is never silent.

    Every peer lands in exactly one bucket (``failed`` is orthogonal: a
    peer whose card write raised is recorded there instead of its verdict
    bucket).
    """

    drifted: tuple[str, ...] = ()
    undetermined: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def summary_line(self) -> str:
        """One human line naming what happened to the board this run."""
        parts = [
            f"{len(self.drifted)} drift card(s)",
            f"{len(self.undetermined)} unknown card(s)",
            f"{len(self.cleared)} cleared",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} DELIVERY-FAILED")
        return "scitex-todo alarm: " + ", ".join(parts)


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _drift_card(peer: str, report: PeerSyncReport) -> tuple[str, str]:
    """Title + note for a peer that DIFFERS from the centre.

    The note names the concrete drift (``report.summary()`` already spells
    out behind/ahead/diverged/dirty) and the exact next command, so the
    operator reading only the card knows what to do.
    """
    title = f"[host-sync] {peer} DRIFTED from centre — {report.summary()}"
    note = (
        f"peer: {peer}\n"
        f"verdict: {report.summary()}\n"
        f"state: {report.state.value}\n"
        f"target: {report.target or '?'}\n"
        f"module: {report.module or '?'}\n\n"
        "This peer is NOT running the centre's code. It is a REPORT, not an "
        "auto-sync — reconcile it deliberately (fast-forward only; sac never "
        "discards remote work):\n"
        f"    sac host sync {peer}\n"
        "Inspect first:\n"
        f"    sac host sync --check {peer}"
    )
    return title, note


def _unknown_card(peer: str, report: PeerSyncReport) -> tuple[str, str]:
    """Title + note for a peer we COULD NOT READ.

    Deliberately worded UNKNOWN, never clean. An undetermined peer is not a
    peer with no drift — it is a peer whose drift we failed to observe, and
    conflating the two is the exact false-clean that has bitten sac before.
    """
    title = f"[host-sync] {peer} UNKNOWN — could not verify ({report.state.value})"
    note = (
        f"peer: {peer}\n"
        f"verdict: {report.summary()}\n"
        f"state: {report.state.value}\n\n"
        "UNKNOWN is NOT clean — sac could not read this peer's code, so its "
        "drift is unobserved, not absent. Nothing was mutated. Restore "
        "reachability, then re-check:\n"
        f"    sac host probe {peer}\n"
        f"    sac host sync --check {peer}"
    )
    return title, note


def _card_exists(store: str | None, card_id: str) -> bool:
    """True when ``card_id`` is already on the board.

    A MISSING store file (nothing carded yet) and a missing id BOTH mean
    "no such card" — the first run against a fresh board raises
    ``FileNotFoundError`` from the loader, the later runs raise
    ``TaskNotFoundError``; neither is an error here.
    """
    from scitex_todo import TaskNotFoundError, get_task

    # stx-allow: fallback (reason: "does this card exist yet?" — a missing store file or missing id is a normal False, not an error; any other load failure propagates to the caller's per-peer guard which reports it loudly)
    try:
        get_task(store, card_id)
        return True
    except (TaskNotFoundError, FileNotFoundError):
        return False


def _upsert(
    store: str | None,
    card_id: str,
    title: str,
    note: str,
    now: datetime | None,
) -> None:
    """Create the card, or update it in place — never duplicate.

    Always (re)asserts ``status=blocked`` / ``blocker=operator-decision``,
    which also REOPENS a card that a previous clean run had resolved: a
    peer that drifts, gets fixed (card resolved), then drifts again must
    alarm again.
    """
    from scitex_todo import add_task, update_task

    if _card_exists(store, card_id):
        update_task(
            store,
            card_id,
            title=title,
            note=note,
            status=_STATUS,
            blocker=_BLOCKER,
            last_activity=_now_iso(now),
        )
    else:
        add_task(
            store,
            id=card_id,
            title=title,
            status=_STATUS,
            blocker=_BLOCKER,
            note=note,
            scope=f"agent:{_AGENT}",
            assignee=_AGENT,
            created_by=_AGENT,
        )


def _clear(store: str | None, card_id: str) -> bool:
    """Resolve the peer's card if present + still open. Returns did-clear.

    Idempotent: no card, or an already-resolved card, is a quiet no-op
    (a clean peer that was never drifted must not create then resolve a
    phantom card).
    """
    from scitex_todo import get_task, resolve_task

    if not _card_exists(store, card_id):
        return False
    existing = get_task(store, card_id)
    if existing.get("status") == "done":
        return False
    resolve_task(store, card_id, actor=_AGENT)
    return True


def route_reports_to_cards(
    results: list[SyncResult],
    *,
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> AlarmOutcome:
    """Upsert / resolve one scitex-todo card per peer from ``results``.

    ``results`` are the read-only verdicts ``sac host sync --check``
    produces. For each peer this upserts a drift or unknown card, or
    resolves the peer's existing card when it is clean. Never raises: a
    per-peer card-write failure is printed loudly to ``err_stream`` and the
    peer is recorded in :attr:`AlarmOutcome.failed`, so one unreachable
    board never suppresses the rest of the fleet's alarms.

    Parameters
    ----------
    store
        scitex-todo store path. ``None`` = the resolved canonical store
        (``~/.scitex/todo/tasks.yaml`` on the centre). Tests pass a real
        temp path — no mocks.
    now, err_stream
        Test seams: a fixed clock and a replacement stderr.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    drifted: list[str] = []
    undetermined: list[str] = []
    cleared: list[str] = []
    failed: list[str] = []

    for result in results:
        peer = result.peer
        report = result.before
        card_id = card_id_for(peer)
        # stx-allow: fallback (reason: card delivery is a SIDE rail — one peer's board-write failure must never crash the drift check that feeds it; it is reported loudly on stderr and recorded in AlarmOutcome.failed)
        try:
            if report.is_undetermined:
                _upsert(store, card_id, *_unknown_card(peer, report), now=now)
                undetermined.append(peer)
            elif report.is_drifted:
                _upsert(store, card_id, *_drift_card(peer, report), now=now)
                drifted.append(peer)
            elif _clear(store, card_id):
                cleared.append(peer)
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            failed.append(peer)
            print(
                f"[host-sync-alarm] {peer}: scitex-todo card delivery FAILED "
                f"— {exc} (drift verdict unchanged; the --check exit code "
                "still reflects it)",
                file=stream,
            )

    return AlarmOutcome(
        drifted=tuple(drifted),
        undetermined=tuple(undetermined),
        cleared=tuple(cleared),
        failed=tuple(failed),
    )
