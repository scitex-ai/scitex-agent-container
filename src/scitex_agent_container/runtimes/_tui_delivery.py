"""Handing a turn to a live TUI pane, and refusing when it would be parked.

Extracted from ``tui_session.py`` (which was over the line limit) because this
is a cohesive responsibility with its own correctness question the rest of that
module does not share: WILL THIS PANE RUN WHAT I HAND IT?

THE DEFECT THIS EXISTS TO CLOSE. Until 2026-08-18 delivery returned True on the
strength of having sent keystrokes, and nothing asked whether the application
took them. The chain that produced was four affirmative signals and no work:

    send_turn types into a busy pane        -> returns True
      -> turn bridge answers 200 {"delivered": true, "text": ""}
        -> `sac peer post-turn` exits 0
          -> nothing runs, and no layer is lying

Measured: four dispatches across four agent states (wedged/resumed,
plain-restarted, fresh-started, and confirmed-not-spinning), zero completions,
including one 35-minute window with no restart in it — long past any plausible
compaction pause, which was the alternative explanation worth ruling out.

WHY IT MATTERS BEYOND RELIABILITY. The operator's standing rule is to use the
cheap local-model agents first and escalate when work does not progress. An
unobservable non-progress condition means that escalation can never trigger, so
the rule silently cannot be implemented. A delivery primitive that cannot report
failure disables the policy above it.

REFUSAL, NOT WAITING. This returns False rather than blocking until the pane
frees up. The caller — a dispatcher, a sweep, an operator — is the one holding
the context to decide between retrying, choosing another agent, and escalating.
Blocking here would hide an unbounded wait inside a call every caller believes
is fast.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import _pane_acceptance
from ._tui_compose import verify_submit_by_advancement

logger = logging.getLogger(__name__)


def send_turn_to_pane(
    mux: Any,
    name: str,
    text: str,
    *,
    wait_ready: bool = True,
    require_accepting: bool = True,
    ensure_ready: Callable[[], None] | None = None,
    submission_poll_s: float = 0.2,
    submission_appear_timeout_s: float = 2.0,
    submission_idle_wait_s: float = 3.0,
    submission_proof_stable_s: float = 0.6,
    submission_max_resends: int = 3,
) -> bool:
    """Deliver one turn to ``name``'s pane; False when it was NOT delivered.

    THE DRAIN AND THE ACCEPTANCE CHECK ARE SEPARATE FLAGS ON PURPOSE, and
    conflating them is how the first version of this fix missed the path that
    matters. The turn bridge — the route ``sac peer post-turn`` actually takes
    — passes ``wait_ready=False`` for a documented and still-valid reason: the
    drain blocks up to 60s waiting on a marker an idle autonomous pane may
    never render, which is fatal for a wake POST. If acceptance rode on the
    same flag, every bridge dispatch would skip it and the fix would cover only
    the paths that were not broken.

    They are also different in cost, which is why one can be default-on: the
    drain BLOCKS; the acceptance check is a single ``capture_content``.

    Args:
        mux: multiplexer exposing ``exists`` / ``capture_content`` /
            ``send_text_literal`` / ``send_keys``.
        name: tmux session name.
        text: the turn.
        wait_ready: run the blocking modal drain first.
        require_accepting: refuse when the pane would PARK the turn. Default
            on, including when ``wait_ready`` is False. Set False only for the
            in-memory unit suite, whose fake renders no status bar and would
            therefore always read as not-accepting.
        ensure_ready: callable that drains any first-launch / mid-session modal.

    Returns:
        True only after the pasted turn observably advanced out of the live
        composer. Merely writing the text and an Enter is not delivery.
    """
    if not mux.exists(name):
        # No runtime to deliver to — distinct from "delivered", and the caller
        # needs that distinction to tell a dead agent from a busy one.
        return False

    if wait_ready and ensure_ready is not None:
        ensure_ready()

    if require_accepting:
        # Read the pane AFTER any drain: a modal on screen would otherwise be
        # read as "not accepting" for the wrong reason.
        content = mux.capture_content(name)
        if not _pane_acceptance.is_accepting(content):
            logger.warning(
                "send_turn REFUSED for %s: %s — the turn was NOT delivered",
                name,
                _pane_acceptance.refusal_reason(content),
            )
            return False

    # Paste and submit are deliberately separate.  A live Codex incident on
    # 2026-09-17 proved that tmux can accept both writes while the TUI drops
    # Enter: the CI notification remained visibly parked in the composer, but
    # the bridge returned success and Cards durably ACKed it.  The same false
    # success also blocks later CCT turns behind the parked text.
    mux.send_text_literal(name, text)
    submitted = verify_submit_by_advancement(
        name,
        capture_fn=mux.capture_content,
        send_keys_fn=lambda key: mux.send_keys(name, key),
        pending_fragment=text,
        max_resends=submission_max_resends,
        poll_s=submission_poll_s,
        appear_timeout_s=submission_appear_timeout_s,
        idle_wait_s=submission_idle_wait_s,
        escape_before_enter=True,
        proof_stable_s=submission_proof_stable_s,
        require_submission_proof=True,
    )
    if not submitted:
        logger.error(
            "send_turn FAILED for %s: payload stayed pasted-but-unsent; "
            "upstream must retain and retry this message",
            name,
        )
    return bool(submitted)


def why_not_deliverable(mux: Any, name: str) -> str | None:
    """Why a turn to ``name`` would not be delivered now, or None if it would.

    Exists so a CALLER can put the specific reason in its own error. The turn
    bridge previously raised "TUI session ... does not exist" for every False
    from send_turn, which was accurate when absence was the only cause and
    became a MISDIAGNOSIS the moment a second cause existed: a busy pane is
    not a missing session, and telling an operator otherwise sends them to
    look for a dead agent that is in fact working.

    An error that names the wrong cause is worse than one that names none,
    because it is actionable in the wrong direction.
    """
    if not mux.exists(name):
        return "no tmux session for this agent — nothing to deliver to"
    return _pane_acceptance.refusal_reason(mux.capture_content(name))


def capture_pane_logs(mux: Any, name: str, lines: int = 50) -> str:
    """Last ``lines`` of pane output; empty string when the session is absent.

    Empty-on-absent rather than raising, because callers distinguish the two by
    asking ``is_running`` first — but note that makes "" ambiguous between "no
    session" and "a session with no output", which is why it is not a liveness
    signal.
    """
    if not mux.exists(name):
        return ""
    return str(mux.capture_logs(name, lines=lines))
