"""Route worktree-sprawl over-cap verdicts to a SEEN surface.

A GC that quietly reaps what it can and says nothing about what it could
NOT reap is how a repo reaches 105 worktrees while a green cron line
scrolls past every night. The reaping is the easy half; the half that
prevents the incident is SHOUTING about the worktrees the predicate
refused to touch, because those are the ones that accumulate forever.

So: after a pass, a repo still over its cap upserts an IDEMPOTENT
scitex-todo card on the board's BLOCKING-YOU view (``status=blocked``,
``blocker=operator-decision`` — the surface the operator already
watches), and the card is RESOLVED the moment the repo drops back under
so a fixed repo stops shouting. Card id is stable per repo
(``worktree-sprawl-<repo-basename>``), so a nightly re-run updates in
place instead of tiling the board with duplicates.

Three-state honest, like every rail here:

* **OVER CAP** → a card naming the repo, the count, and the kept-reasons
  breakdown (``9 dirty, 6 unmerged, 2 in-use``). The breakdown IS the
  card's value: "17 worktrees" is a number, "9 dirty" is an instruction.
* **UNREADABLE** (not a git repo, git missing, unreadable path) → a card
  too, labelled UNKNOWN. "I could not look" must never read as "I looked
  and it was fine".
* **UNDER CAP** → resolve the repo's card.

Delivery is a SIDE RAIL. A board-write failure prints loudly to stderr
and is recorded in :attr:`AlarmOutcome.failed`; it never crashes the GC
that feeds it, and one unreachable board never suppresses the rest of the
fleet's alarms. Mirrors ``_hostsync._alarm`` (the drift rail) deliberately
— same conventions, same public ``scitex_todo`` writer, no shared import.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._worktree_gc_model import RepoGcResult

__all__ = ["AlarmOutcome", "CARD_ID_PREFIX", "card_id_for", "route_gc_to_cards"]

#: Stable per-repo card id prefix, so a re-run updates in place instead of
#: duplicating (idempotency is by id).
CARD_ID_PREFIX = "worktree-sprawl-"

#: The card's owner + author. A pseudo-agent, not a human: the card is a
#: standing "this repo is over its worktree cap" fact that the operator
#: resolves. Mirrors the host-sync drift rail's convention.
_AGENT = "sac.worktree-gc"

#: An over-cap card and an unreadable-repo card BOTH land on the board's
#: BLOCKING-YOU view (``status=blocked`` AND ``blocker=operator-decision``)
#: — the seen surface. The card TEXT distinguishes sprawl from unknown.
_STATUS = "blocked"
_BLOCKER = "operator-decision"


def card_id_for(repo: str | Path) -> str:
    """The stable card id for ``repo``'s sprawl alarm.

    Keyed on the repo's BASENAME so the id stays readable on the board.
    Two checkouts of the same repo under different parents collide by
    design: the card is about "the repo called scitex-agent-container",
    which is how the operator thinks about it, and the note carries the
    full path either way.
    """
    name = Path(str(repo)).name or "repo"
    return f"{CARD_ID_PREFIX}{name}"


@dataclass(frozen=True)
class AlarmOutcome:
    """What the routing did this run — so the caller is never silent.

    Every repo lands in exactly one bucket (``failed`` is orthogonal: a
    repo whose card write raised is recorded there instead of its verdict
    bucket).
    """

    exceeded: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    cleared: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def summary_line(self) -> str:
        """One human line naming what happened to the board this run."""
        parts = [
            f"{len(self.exceeded)} over-cap card(s)",
            f"{len(self.unreadable)} unknown card(s)",
            f"{len(self.cleared)} cleared",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} DELIVERY-FAILED")
        return "scitex-todo alarm: " + ", ".join(parts)


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _breakdown(result: RepoGcResult) -> str:
    counts = result.keep_reason_breakdown
    if not counts:
        return "(no kept worktrees)"
    return ", ".join(f"{n} {reason}" for reason, n in counts.items())


def _over_cap_card(result: RepoGcResult) -> tuple[str, str]:
    """Title + note for a repo STILL over its cap after the pass.

    The note names the concrete breakdown and the exact next commands, so
    the operator reading only the card knows what to do.
    """
    title = (
        f"[worktree-gc] {Path(result.repo).name} OVER CAP — "
        f"{result.count_after} worktrees (cap {result.cap})"
    )
    note = (
        f"repo: {result.repo}\n"
        f"worktrees: {result.count_after} (cap {result.cap})\n"
        f"removed this pass: {len(result.removed)}\n"
        f"kept: {len(result.kept)} — {_breakdown(result)}\n\n"
        "The GC removed everything it could PROVE was safe (clean AND "
        "merged AND older than the age gate AND not in use). The kept "
        "worktrees each failed at least one of those legs — the breakdown "
        "above says which. They will never be auto-removed; a human decides.\n\n"
        "Inspect (read-only, removes nothing):\n"
        f"    sac worktree gc --repo {result.repo}\n"
        "Then, per worktree: land or drop the branch, or clean the tree.\n"
        "Dirty worktrees hold work that exists NOWHERE else — commit or "
        "push before removing anything by hand."
    )
    return title, note


def _unknown_card(result: RepoGcResult) -> tuple[str, str]:
    """Title + note for a repo we COULD NOT READ.

    Deliberately worded UNKNOWN, never clean. An unreadable repo is not a
    repo without sprawl — it is a repo whose sprawl we failed to observe.
    """
    title = f"[worktree-gc] {Path(result.repo).name} UNKNOWN — could not read repo"
    note = (
        f"repo: {result.repo}\n"
        f"error: {result.error}\n\n"
        "UNKNOWN is NOT clean — sac could not enumerate this repo's "
        "worktrees, so its sprawl is unobserved, not absent. Nothing was "
        "removed. Check the path still exists and is a git repo, then "
        "re-run:\n"
        f"    sac worktree gc --repo {result.repo}"
    )
    return title, note


def _card_exists(store: str | None, card_id: str) -> bool:
    """True when ``card_id`` is already on the board.

    A MISSING store file (nothing carded yet) and a missing id BOTH mean
    "no such card" — the first run against a fresh board raises
    ``FileNotFoundError`` from the loader, later runs raise
    ``TaskNotFoundError``; neither is an error here.
    """
    from scitex_todo import TaskNotFoundError, get_task

    # stx-allow: fallback (reason: "does this card exist yet?" — a missing store file or missing id is a normal False, not an error; any other load failure propagates to the caller's per-repo guard which reports it loudly)
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
    which also REOPENS a card a previous under-cap run had resolved: a
    repo that sprawls, gets cleaned (card resolved), then sprawls again
    must alarm again.
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
    """Resolve the repo's card if present + still open. Returns did-clear.

    Idempotent: no card, or an already-resolved card, is a quiet no-op — a
    healthy repo that was never over cap must not create then resolve a
    phantom card.
    """
    from scitex_todo import get_task, resolve_task

    if not _card_exists(store, card_id):
        return False
    existing = get_task(store, card_id)
    if existing.get("status") == "done":
        return False
    resolve_task(store, card_id, actor=_AGENT)
    return True


def route_gc_to_cards(
    results: list[RepoGcResult],
    *,
    store: str | None = None,
    now: datetime | None = None,
    err_stream: Any = None,
) -> AlarmOutcome:
    """Upsert / resolve one scitex-todo card per repo from ``results``.

    Never raises: a per-repo card-write failure is printed loudly to
    ``err_stream`` and the repo is recorded in :attr:`AlarmOutcome.failed`,
    so a broken board never takes down the GC pass that feeds it.

    Parameters
    ----------
    store
        scitex-todo store path. ``None`` = the resolved canonical store
        (``~/.scitex/todo/tasks.yaml``). Tests pass a real temp path — no
        mocks.
    now, err_stream
        Test seams: a fixed clock and a replacement stderr.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    exceeded: list[str] = []
    unreadable: list[str] = []
    cleared: list[str] = []
    failed: list[str] = []

    for result in results:
        repo = result.repo
        card_id = card_id_for(repo)
        # stx-allow: fallback (reason: card delivery is a SIDE rail — one repo's board-write failure must never crash the GC pass that feeds it; it is reported loudly on stderr and recorded in AlarmOutcome.failed)
        try:
            if result.unreadable:
                _upsert(store, card_id, *_unknown_card(result), now=now)
                unreadable.append(repo)
            elif result.exceeds_cap:
                _upsert(store, card_id, *_over_cap_card(result), now=now)
                exceeded.append(repo)
            elif _clear(store, card_id):
                cleared.append(repo)
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            failed.append(repo)
            print(
                f"[worktree-gc-alarm] {repo}: scitex-todo card delivery FAILED "
                f"— {exc} (the GC pass itself is unaffected; its exit code "
                "still reflects the cap verdict)",
                file=stream,
            )

    return AlarmOutcome(
        exceeded=tuple(exceeded),
        unreadable=tuple(unreadable),
        cleared=tuple(cleared),
        failed=tuple(failed),
    )
