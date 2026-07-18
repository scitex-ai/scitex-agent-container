"""One board card per open PR — sac feeds FACTS, scitex-todo does the nudging.

THE SSOT SPLIT (read this before adding a reminder here)
--------------------------------------------------------
scitex-todo ALREADY owns nudging. Its stale-active sweep nudges the owner of
any open card left untouched past the threshold — unprompted, and demonstrably
working (it nudged one agent 24 and then 26 cards in a single night, 2026-07-18).

So this module contains NO nudge logic, no reminder schedule and no escalation
ladder, and adding one would be a second nudger with independent state racing
the first — the same double-supervisor class as the ``sac.listen``
near-catastrophe recorded in :mod:`.._jobs_plugin`.

sac's job is narrower and unowned: make every open PR VISIBLE ON THE BOARD as a
card, so scitex-todo has something to nudge about at all. The operator's own
diagnosis of the 2026-07-18 backlog was exactly this gap —「カードになってない
か追跡できてない、催促できてないのも悪い」 — and the missing link was the
first clause, not the third.

IDEMPOTENCY
-----------
The card id is derived from ``(repo, number)`` (:func:`card_id_for`), so a
re-run updates in place and can never duplicate. A PR that merges or closes
drops out of the open list and its card is COMPLETED — but ONLY on a pass that
proved it could read the list. See :func:`sync_cards` in :mod:`._sweep` for why
that guard is the difference between bookkeeping and mass card-deletion.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "CARD_ID_PREFIX",
    "CardWrite",
    "HEARTBEAT_CARD_ID",
    "MANIFEST_CARD_ID",
    "card_id_for",
    "complete_card",
    "open_card_numbers",
    "upsert_pr_card",
    "upsert_sweep_heartbeat",
]

#: Stable card-id prefix. Greppable on the board, and the anchor
#: :func:`open_card_numbers` uses to find cards whose PR is gone.
CARD_ID_PREFIX = "sac-pr-"

#: The sweep's own liveness beacon — "who watches the watcher". Kept OPEN so
#: scitex-todo's stale-active nudge shouts if this sweep ever stops ticking.
HEARTBEAT_CARD_ID = "sac-pr-card-sweep-heartbeat"

#: The manifest of the 2026-07-18 hand-applied force-close, which the expiry
#: job's close comment points at so a surprised author can read the policy and
#: see they were not singled out.
MANIFEST_CARD_ID = "sac-pr-3day-expiry-force-close-manifest-20260718"

#: Owner of the per-PR cards: a pseudo-agent, so the cards are attributable to
#: the mechanism rather than to whichever human happened to run it.
_AGENT = "sac.sync-pr-cards"

#: The heartbeat is owned by the PACKAGE identity — a human must own noticing
#: that this sweep went quiet.
_HEARTBEAT_ASSIGNEE = "scitex-agent-container"


def card_id_for(repo: str, number: int) -> str:
    """The stable card id for one PR. Derived ONLY from ``(repo, number)``.

    Both parts matter: the number alone would collide across repos, and a
    collision here means two PRs sharing one card — one of them invisible.
    """
    slug = repo.replace("/", "-").strip("-").lower()
    return f"{CARD_ID_PREFIX}{slug}-{number}"


def _now_iso(now: "datetime | None") -> str:
    """A timestamp in scitex-todo's canonical shape (second-res, ``Z``)."""
    stamp = now or datetime.now(timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CardWrite:
    """What one card write did — so a caller is never silent about it."""

    card_id: str
    number: int
    action: str  # "upserted" | "completed" | "failed"
    detail: str = ""


def _card_exists(store, card_id: str) -> bool:
    from scitex_todo import TaskNotFoundError, get_task

    # stx-allow: fallback (reason: "does this card exist yet?" — a missing store or id is a normal False; any other failure propagates to the caller's per-card guard, which reports it loudly)
    try:
        get_task(store, card_id)
        return True
    except (TaskNotFoundError, FileNotFoundError):
        return False


def _upsert(
    store,
    card_id: str,
    title: str,
    note: str,
    now,
    *,
    status: str,
    assignee: str,
) -> None:
    """Create the card, or update it in place — never duplicate.

    Re-asserting ``status`` also REOPENS a card an earlier pass completed: a PR
    that closes (card completed) and is then REOPENED must come back onto the
    board rather than stay invisible.
    """
    from scitex_todo import add_task, update_task

    if _card_exists(store, card_id):
        update_task(
            store,
            card_id,
            title=title,
            note=note,
            status=status,
            last_activity=_now_iso(now),
        )
    else:
        add_task(
            store,
            id=card_id,
            title=title,
            status=status,
            note=note,
            scope=f"agent:{assignee}",
            assignee=assignee,
            created_by=_AGENT,
        )


def _title(pr, now: datetime) -> str:
    """The card title — the facts a human triages on, without opening it."""
    flags = []
    if pr.draft:
        flags.append("draft")
    if pr.ci and pr.ci != "success":
        flags.append(f"ci:{pr.ci}")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"[pr] {pr.repo}#{pr.number} ({int(pr.age_days(now))}d) "
        f"{pr.title.strip()[:110]}{suffix}"
    )


def _note(pr, now: datetime) -> str:
    """The card body — FACTS ONLY.

    Note what is deliberately NOT here: any verdict about shelf life. Age is a
    measurement and belongs to sac; "is this PR too old" is a FLEET-WIDE rule
    that scitex-dev owns (see :mod:`._expiry_seam`), and printing sac's own
    arithmetic against a local threshold would be that primitive forked into a
    card body — the quietest possible place to fork it.

    So the card reports HOW OLD the PR is and points at the rule. It does not
    adjudicate it.
    """
    return (
        f"pr:       {pr.repo}#{pr.number}\n"
        f"title:    {pr.title.strip()}\n"
        f"author:   {pr.author}\n"
        f"url:      {pr.url}\n"
        f"opened:   {pr.created_at} ({pr.age_days(now):.1f} day(s) ago)\n"
        f"updated:  {pr.updated_at} ({pr.idle_days(now):.1f} day(s) ago)\n"
        f"draft:    {'yes' if pr.draft else 'no'}\n"
        f"ci:       {pr.ci}\n\n"
        "This card exists so this PR is TRACKABLE — one card per open PR, "
        "upserted by sac's PR-card sweep. It is completed automatically when "
        "the PR merges or closes.\n\n"
        "sac does NOT nudge from here. scitex-todo's stale-active sweep already "
        "nudges the owner of any open card left untouched, and a second nudger "
        "would just race the first. sac supplies the FACT; scitex-todo supplies "
        "the reminder.\n\n"
        "SHELF LIFE is a FLEET-WIDE rule owned by scitex-dev, not by sac and "
        "not by this repo (operator, 2026-07-18: 「3日ルールは全てのレポジトリ"
        "で共通です」). The ages above are the measurement; the rule that reads "
        "them lives in dev's primitive. sac deliberately does not adjudicate it "
        "here — see the _expiry_seam module.\n\n"
        "Review it:\n"
        f"    gh pr view {pr.number} -R {pr.repo}\n"
        f"    gh pr diff {pr.number} -R {pr.repo}"
    )


def upsert_pr_card(pr, *, store=None, now: "datetime | None" = None) -> CardWrite:
    """Upsert THE card for one open PR. Never raises.

    A board-write failure is reported as an ``action="failed"`` write rather
    than thrown, so one unwritable card can never suppress the rest of the
    backlog's cards.
    """
    stamp = now or datetime.now(timezone.utc)
    card_id = card_id_for(pr.repo, pr.number)
    # stx-allow: fallback (reason: card delivery is bookkeeping — one PR's board-write failure must never crash the sweep nor hide the other PRs; it is recorded as a FAILED write the caller reports and exits non-zero on)
    try:
        _upsert(
            store,
            card_id,
            _title(pr, stamp),
            _note(pr, stamp),
            stamp,
            # in_progress, NOT blocked: an open PR is work in flight, and only
            # an OPEN card is watched by scitex-todo's stale-active nudge —
            # which is the entire reason this card exists.
            status="in_progress",
            assignee=_AGENT,
        )
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return CardWrite(card_id, pr.number, "failed", f"card write FAILED: {exc}")
    return CardWrite(card_id, pr.number, "upserted", f"{pr.repo}#{pr.number}")


def complete_card(repo: str, number: int, *, store=None, reason: str = "") -> CardWrite:
    """Complete the card for a PR that is no longer open. Never raises.

    Called ONLY from a pass that proved it could read the open-PR list — see
    the guard in :func:`.._sweep.sync_cards`.
    """
    card_id = card_id_for(repo, number)
    # stx-allow: fallback (reason: bookkeeping — a failed completion must not crash the sweep; it is recorded as FAILED and reported)
    try:
        from scitex_todo import comment_task, complete_task

        if reason:
            # stx-allow: fallback (reason: the audit comment is decoration on top of the completion — its failure must not prevent the completion itself)
            try:
                comment_task(store, card_id, reason, by=_AGENT)
            except Exception:
                pass
        complete_task(store, card_id, by=_AGENT)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return CardWrite(card_id, number, "failed", f"card completion FAILED: {exc}")
    return CardWrite(card_id, number, "completed", reason or f"{repo}#{number}")


def open_card_numbers(repo: str, *, store=None) -> "dict | None":
    """``{pr_number: card_id}`` for every OPEN pr-card of ``repo``.

    THREE-state, for the same reason as everything else here:

    * ``{...}`` / ``{}`` — we READ the board. An empty dict genuinely means
      "this repo has no open pr-cards".
    * ``None`` — the board could not be read. The caller must NOT conclude "no
      cards exist" from that, or a board hiccup completes nothing (harmless) —
      or worse, a future refactor reads it as "every card is gone".

    A store file that does not exist yet is the FIRST RUN, not a failure: there
    are demonstrably no cards, which is a real answer. Collapsing that into
    ``None`` would print a scary "BOARD could not be read" on every pass of a
    fresh install and train the operator to ignore the one message that matters
    when it is real.
    """
    from scitex_todo import list_tasks

    prefix = f"{CARD_ID_PREFIX}{repo.replace('/', '-').strip('-').lower()}-"
    # stx-allow: fallback (reason: this IS the three-state board read — an ABSENT store is a genuine first-run empty, while any other failure is UNKNOWN (None) so the caller skips completion rather than guessing that every card's PR is gone)
    try:
        rows = list_tasks(store, id_prefix=prefix)
    except FileNotFoundError:
        return {}
    except Exception:
        return None
    out: dict = {}
    for row in rows:
        card_id = str(row.get("id", ""))
        if not card_id.startswith(prefix) or row.get("status") == "done":
            continue
        tail = card_id[len(prefix) :]
        if tail.isdigit():
            out[int(tail)] = card_id
    return out


def _heartbeat_note(stats, *, mode: str, repos, detail: str, now) -> str:
    counted = "\n".join(f"  {k:<14} {v}" for k, v in stats.items()) or "  (nothing)"
    return (
        f"last run: {_now_iso(now)}\n"
        f"mode:     {mode}\n"
        f"repos:    {', '.join(repos) or '(none)'}\n"
        f"fetch:    {detail}\n\n"
        f"{counted}\n\n"
        "This card is the PR-card sweep's LIVENESS BEACON, refreshed on every "
        "pass — including passes that change nothing, which are the ticks that "
        "prove the mechanism is alive at all.\n\n"
        "IF THIS CARD IS STALE, THE SWEEP IS NOT RUNNING and open PRs are once "
        "again accumulating untracked — which is precisely how 31 of 35 PRs "
        "reached the point of being force-closed by hand on 2026-07-18.\n\n"
        "Check the timer:\n"
        "    systemctl --user list-timers | grep sync-pr-cards\n"
        "Run a pass by hand (read-only):\n"
        "    sac pr sync-cards --check"
    )


def upsert_sweep_heartbeat(
    stats,
    *,
    mode: str,
    repos=(),
    detail: str = "",
    store=None,
    now: "datetime | None" = None,
    err_stream: Any = None,
) -> bool:
    """Refresh the sweep's own liveness card. Returns did-write.

    A SIDE rail: a board-write failure prints loudly and returns ``False``; it
    never raises into the pass.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    # stx-allow: fallback (reason: the heartbeat is a SIDE rail — telling the board we are alive must never crash the sweep that feeds it; the failure is printed loudly and returned)
    try:
        _upsert(
            store,
            HEARTBEAT_CARD_ID,
            f"[pr] sac PR-card sweep heartbeat — last {mode} pass {_now_iso(now)}",
            _heartbeat_note(stats, mode=mode, repos=repos, detail=detail, now=now),
            now,
            status="in_progress",
            assignee=_HEARTBEAT_ASSIGNEE,
        )
        return True
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[pr-card-sweep] HEARTBEAT card delivery FAILED — {exc}. The sweep "
            f"itself was UNAFFECTED, but the board can no longer tell anyone "
            f"whether this sweep is alive.",
            file=stream,
        )
        return False
