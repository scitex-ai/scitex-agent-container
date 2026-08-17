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

logger = logging.getLogger(__name__)


def send_turn_to_pane(
    mux: Any,
    name: str,
    text: str,
    *,
    wait_ready: bool = True,
    ensure_ready: Callable[[], None] | None = None,
) -> bool:
    """Deliver one turn to ``name``'s pane; False when it was NOT delivered.

    Args:
        mux: multiplexer exposing ``exists`` / ``capture_content`` /
            ``send_text_and_submit``.
        name: tmux session name.
        text: the turn.
        wait_ready: drain modals and check acceptance first. False skips BOTH,
            for the in-memory unit suite whose fake renders neither.
        ensure_ready: callable that drains any first-launch / mid-session modal
            before the pane is read.

    Returns:
        True only when the keystrokes were sent to a pane that will run them.
    """
    if not mux.exists(name):
        # No runtime to deliver to — distinct from "delivered", and the caller
        # needs that distinction to tell a dead agent from a busy one.
        return False

    if wait_ready:
        if ensure_ready is not None:
            ensure_ready()
        # Read the pane AFTER draining: the drain is what makes readiness
        # meaningful, and busy/queued state can only be judged once no modal
        # covers it.
        content = mux.capture_content(name)
        if not _pane_acceptance.is_accepting(content):
            logger.warning(
                "send_turn REFUSED for %s: %s — the turn was NOT delivered",
                name,
                _pane_acceptance.refusal_reason(content),
            )
            return False

    mux.send_text_and_submit(name, text)
    return True


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
