"""CompactAction — request a /compact and verify it via context_pct drop.

Why a separate completion signal matters
----------------------------------------
A probe that checks for a string in the pane (e.g. "Conversation
compacted") would be brittle — Claude Code's TUI wording changes
between releases, and the status message can scroll off before the
poll sees it. Using the statusline-reported ``context_pct`` gives a
numeric, unambiguous "did the compact succeed?" signal.

Outcome interpretation
----------------------
- ``SUCCESS``             — ``context_pct`` dropped by at least
  ``min_drop_pct`` (default 20 percentage points).
- ``COMPLETION_TIMEOUT``  — context didn't drop enough in time.
  Could mean: (a) compact hasn't finished, (b) ``context_pct_fn``
  returned ``None`` throughout (statusline unavailable), (c) the
  compact was rejected by Claude Code. Operator inspects the
  ``pane_after`` log column.
- ``PRECONDITION_FAIL``   — pane was busy; we declined to
  interrupt an in-flight turn.
- ``SEND_ERROR``          — ``send_text_and_submit`` itself raised.

Operators who want a scheduled auto-compact (interval or
threshold) should build that policy layer *on top of* this action —
it is intentionally policy-free.
"""

from __future__ import annotations

from typing import Any, Optional

from ..action_base import ActionContext, PaneAction
from ..liveness_probe import pane_is_busy

# Tail window carried into the attempt log for forensic readers.
# Compacts don't need the huge window a nonce probe needs because
# the completion signal is numeric, not textual.
_PANE_TAIL_CHARS = 2000

# Default minimum context-pct drop to declare SUCCESS. Compacts
# typically clear 30-70pp; 20 is a conservative floor that rejects
# the "compact ran but barely reclaimed anything" case as still
# worth reporting but not as SUCCESS.
_DEFAULT_MIN_DROP_PCT = 20.0


class CompactAction(PaneAction):
    """Send ``/compact`` and wait for ``context_pct`` to fall.

    Parameters
    ----------
    min_drop_pct:
        Percentage points the context must drop for this action to
        report ``SUCCESS``. Default 20. Tune per operator
        preference; higher = stricter.
    command:
        Override the submitted text. Default ``"/compact"``. Present
        for tests that want to exercise the flow without assuming
        a specific Claude Code command string.
    """

    name = "compact"

    def __init__(
        self,
        min_drop_pct: float = _DEFAULT_MIN_DROP_PCT,
        command: str = "/compact",
    ):
        self.min_drop_pct = float(min_drop_pct)
        self._command = command

    # ---- PaneAction surface ----------------------------------------

    def snapshot(self, ctx: ActionContext) -> dict[str, Any]:
        pane = ctx.capture_fn() or ""
        return {
            "pane_tail": pane[-_PANE_TAIL_CHARS:],
            "context_pct": _coerce_float(ctx.context_pct_fn()),
        }

    def precheck(self, before: dict[str, Any]) -> bool:
        """Refuse to compact a currently-busy pane.

        Two separate reasons:
          1. Interrupting an in-flight response would corrupt the
             user's work (same as the nonce-probe rationale).
          2. Claude Code occasionally ignores ``/compact`` when it
             is mid-tool-call; the command lands but the TUI does
             not act on it until the current turn resolves,
             producing a confusing COMPLETION_TIMEOUT with no
             actual failure.
        """
        return not pane_is_busy(before.get("pane_tail", ""))

    def send(self, ctx: ActionContext) -> None:
        ctx.mux.send_text_and_submit(ctx.session, self._command)
        # Record the command we actually sent so the attempt log is
        # self-describing for anyone debugging why a compact didn't
        # match expectations (custom Claude Code build, alias, etc.).
        ctx.extras["command"] = self._command

    def is_complete(self, before: dict[str, Any], now: dict[str, Any]) -> bool:
        """``True`` iff ``context_pct`` dropped by at least
        ``min_drop_pct`` percentage points between the before and
        now snapshots.

        Either side being ``None`` (statusline unavailable or not
        yet populated) always returns ``False`` — the engine then
        either keeps polling until the deadline or reports
        ``COMPLETION_TIMEOUT``. The attempt log still captures
        both snapshots so the operator can see *why* we couldn't
        confirm.
        """
        b = _coerce_float(before.get("context_pct"))
        n = _coerce_float(now.get("context_pct"))
        if b is None or n is None:
            return False
        return (b - n) >= self.min_drop_pct


def _coerce_float(value: Any) -> Optional[float]:
    """Tolerant float coercion — returns ``None`` for ``None`` /
    bad input. The statusline parser may emit strings or ``None``
    depending on the Claude Code build, so be forgiving."""
    if value is None:
        return None
    # stx-allow: fallback (reason: statusline parser may emit non-numeric or None values)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
