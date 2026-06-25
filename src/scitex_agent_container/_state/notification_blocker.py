"""Record a card blocker when a Claude Code ``Notification`` hook fires.

Claude Code emits a ``Notification`` hook event whenever it is waiting for
input or permission (matcher types ``permission_prompt`` / ``idle_prompt`` /
``auth_success`` / ``elicitation_dialog``). When that happens, the agent is
*blocked on the operator* but typically says nothing — the operator only
discovers it by opening the terminal (the live failure that motivated
``sac-card-anchored-stop-reconciler``: a maintainer sat blocked at a "Submit
answers" prompt unnoticed).

This module is the deterministic backstop: given the notification payload, it
resolves the agent's most-recently-active ``in_progress`` task card and stamps
it ``status=blocked`` / ``blocker=operator-decision`` plus a comment carrying
the notification message — so the board shows the block immediately.

Design rules
------------
- **No ``import scitex_todo``.** The shared task store is mutated ONLY through
  the installed ``scitex-todo`` CLI (``list-tasks`` / ``update`` / ``comment``).
- **Fail-loud, no-surprise.** If the agent owns zero ``in_progress`` cards we
  write nothing to a card and emit a loud agent-level log line instead of
  silently guessing.
- **Dedup.** The per-agent event ring (:mod:`event_log`) records each
  notification; a repeat ``(agent, card, message-hash)`` seen recently is not
  re-commented.
- **Never raise.** A hook handler must not crash the host agent.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import Any

from .event_log import append_event, read_recent

logger = logging.getLogger(__name__)

# How many recent ring events to scan when checking for a duplicate
# notification (keeps dedup O(window), simple, and bounded).
_DEDUP_WINDOW = 50
# The verified scitex-todo blocker enum value for "waiting on the operator to
# decide" — confirmed against scitex_todo._model.VALID_BLOCKERS and the
# board's ``--blocking-me`` predicate (status=blocked AND
# blocker=operator-decision). NOT the card's loose wording.
_BLOCKER_OPERATOR_DECISION = "operator-decision"


def _notification_message(payload: dict[str, Any]) -> str:
    """Extract the human-readable notification text from the hook payload."""
    for key in ("message", "notification", "body", "title"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "Claude Code is waiting for input"


def _message_hash(message: str) -> str:
    """Short stable hash of the notification message for dedup."""
    return hashlib.sha256(message.encode("utf-8", "replace")).hexdigest()[:16]


def _run_todo(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the ``scitex-todo`` CLI; never raises (returns the result)."""
    return subprocess.run(  # noqa: S603,S607 — trusted CLI, fixed argv
        ["scitex-todo", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _most_recent_in_progress(agent: str) -> dict[str, Any] | None:
    """Return the agent's most-recently-active ``in_progress`` card, or None.

    Reads the shared store via ``scitex-todo list-tasks --agent <agent>
    --status in_progress --json`` and picks the row with the latest
    ``last_activity`` (falling back to list order when absent).
    """
    import json

    proc = _run_todo(
        ["list-tasks", "--agent", agent, "--status", "in_progress", "--json"]
    )
    if proc.returncode != 0:
        logger.warning(
            "scitex-todo list-tasks failed for agent %s (rc=%s): %s",
            agent,
            proc.returncode,
            proc.stderr.strip()[:300],
        )
        return None
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("scitex-todo list-tasks returned non-JSON for %s", agent)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("last_activity") or ""), reverse=True)
    return rows[0]


def _already_recorded(agent: str, card_id: str, msg_hash: str) -> bool:
    """True if this (agent, card, message-hash) notification was seen recently."""
    for ev in read_recent(agent, limit=_DEDUP_WINDOW):
        if (
            ev.get("kind") == "notification"
            and ev.get("card_id") == card_id
            and ev.get("message_hash") == msg_hash
        ):
            return True
    return False


def handle_notification(agent: str, payload: dict[str, Any]) -> None:
    """Record the operator-decision blocker on the agent's active card.

    Fail-loud on zero cards; dedup on repeat; never raises.
    """
    # stx-allow: fallback (reason: hook handler must never crash the host
    # agent; any failure resolving the store / running the CLI is logged and
    # swallowed so the tool call is not aborted)
    try:
        message = _notification_message(payload)
        msg_hash = _message_hash(message)

        card = _most_recent_in_progress(agent)
        if card is None:
            # Fail-loud / no-surprise: do NOT silently pick a card.
            logger.warning(
                "Notification hook fired for agent %s but it owns no "
                "in_progress card — nothing recorded (message: %s)",
                agent,
                message[:200],
            )
            # Still ring the event so the agent's heartbeat reflects the
            # notification (no card_id — nothing to dedup against a card).
            append_event(
                agent,
                "notification",
                {"message": message, "message_hash": msg_hash, "card_id": ""},
            )
            return

        card_id = str(card["id"])

        if _already_recorded(agent, card_id, msg_hash):
            logger.info(
                "Notification for agent %s card %s already recorded "
                "(dedup) — skipping re-comment",
                agent,
                card_id,
            )
            return

        # Set the blocker + status on the card.
        upd = _run_todo(
            [
                "update",
                card_id,
                "--status",
                "blocked",
                "--blocker",
                _BLOCKER_OPERATOR_DECISION,
            ]
        )
        if upd.returncode != 0:
            logger.warning(
                "scitex-todo update failed for card %s (rc=%s): %s",
                card_id,
                upd.returncode,
                upd.stderr.strip()[:300],
            )

        # Carry the notification message as a comment on the card.
        comment_text = f"[notification] agent waiting for input: {message}"
        cmt = _run_todo(["comment", card_id, comment_text, "--author", f"agent:{agent}"])
        if cmt.returncode != 0:
            logger.warning(
                "scitex-todo comment failed for card %s (rc=%s): %s",
                card_id,
                cmt.returncode,
                cmt.stderr.strip()[:300],
            )

        # TODO(sac-card-anchored-stop-reconciler): emit a dedicated scitex-todo
        # ``needs-decision`` bus event KIND here so downstream push (phone /
        # telegram / email) can subscribe to the decision-needed signal
        # directly. That is a scitex-todo-side change; for now the comment
        # write above already emits a card-message bus event, which suffices
        # for this increment.

        # Record in the per-agent ring LAST so a future identical notification
        # dedups against this one.
        append_event(
            agent,
            "notification",
            {"message": message, "message_hash": msg_hash, "card_id": card_id},
        )
    except Exception:  # stx-allow: fallback (reason: catch-all — hook handler must never crash the host agent)
        logger.warning("notification_blocker.handle_notification failed", exc_info=True)
