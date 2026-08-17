"""Will this pane RUN a turn handed to it now, or park it?

``prompts.is_ready`` reads like it answers this and does not. It delegates to
``_detect_done``, which keys on the string "bypass permissions" — and the status
bar carries that string the WHOLE TIME, including mid-turn. Verified on live
panes, busy and idle, in one capture: the marker is present in both. So
``is_ready`` is really "no modal is blocking", which is exactly what boot-drain
needs and is correct there, and is the wrong question for dispatch.

WHY THIS MATTERS MORE THAN IT SOUNDS. ``TuiSessionRuntime.send_turn`` gated on
``is_ready``, typed the turn into the pane, pressed Enter and returned True. The
turn bridge answers 200 {"delivered": true} on that True and ``sac peer
post-turn`` exits 0. So a turn dispatched into a busy pane produced four
affirmative signals and no work — and the operator, who was told to escalate
when work does not progress, had no observable non-progress condition to
escalate on.

MEASURED 2026-08-18, four dispatches across four agent states (wedged/resumed,
plain-restarted, fresh-started, and confirmed-not-spinning): zero completed.
The last of those is why this module checks two things rather than one — that
pane was NOT spinning and was holding queued input, so "not busy" alone would
have called it ready and lost the work again.

THREE STATES SHARE THE "LOOKS FINE" APPEARANCE and only one accepts work:

    genuinely idle        runs the turn                      ACCEPTING
    mid-turn (working)    parks it behind the current turn   not accepting
    holding queued input  parks it behind the queue          not accepting
"""

from __future__ import annotations

from .prompts import is_ready

#: A working agent prints an elapsed/token line — "(5m 39s · ↓ 12.2k tokens)" —
#: and a /btw tip whose wording is stable across versions. Keyed on SHAPE, never
#: on the spinner's word: "Slithering", "Bunning" and the rest ROTATE, so a
#: vocabulary-based detector works right up until the word changes and then
#: fails silently in the direction that loses work.
_BUSY_MARKERS = ("tokens)", "without interrupting")

#: Claude Code parks input typed while it is working and says so in the pane. A
#: pane in this state ACCEPTS keystrokes and does not run them, which is the
#: exact failure this module exists to stop.
_QUEUED_MARKER = "Press up to edit queued messages"


def is_busy(content: str) -> bool:
    """True when the agent is mid-turn, detected by shape not by spinner word."""
    return any(marker in content for marker in _BUSY_MARKERS)


def has_queued_input(content: str) -> bool:
    """True when the pane is holding input it has not run yet."""
    return _QUEUED_MARKER in content


def is_accepting(content: str) -> bool:
    """True when a turn handed to this pane now will actually run."""
    return is_ready(content) and not is_busy(content) and not has_queued_input(content)


def refusal_reason(content: str) -> str | None:
    """Why this pane would park a turn, or None when it would run it.

    Returned rather than logged so the CALLER can put it in its refusal — a
    dispatcher that says "not delivered" without saying why sends the operator
    back to the pane to find out, which is the manual step the whole verified
    -delivery change exists to remove.
    """
    if not is_ready(content):
        return "a modal is blocking the input (not at the main prompt)"
    if is_busy(content):
        return "the agent is mid-turn; a turn sent now is parked behind it, not run"
    if has_queued_input(content):
        return "the pane is already holding queued input that has not run"
    return None
