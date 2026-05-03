"""NonceProbeAction — functional-liveness probe via ``Repeat <nonce>``.

A pane-diff is a false liveness signal (channel notifications land
in the terminal even when the local agent is frozen). This action
proves the full ``pane -> LLM -> pane`` loop is working by asking
the agent to echo back a random token and watching for it.

Composition
-----------
- :mod:`..action_base`    — ``PaneAction`` ABC + ``run_action`` engine.
- :mod:`..liveness_probe` — pure helpers (``generate_nonce``,
  ``pane_has_nonce_echo``, ``pane_is_busy``).

Outcome interpretation (via ``ActionOutcome``)
-----------------------------------------------
- ``SUCCESS``             — nonce echoed; agent is functionally alive.
- ``COMPLETION_TIMEOUT``  — nonce never appeared; agent is
  either silent (frozen) or busy beyond our patience. Differentiate
  by inspecting the stored ``pane_after`` for busy markers, or by
  scheduling a second probe after a back-off.
- ``PRECONDITION_FAIL``   — the pane was busy *before* we sent; we
  declined to interrupt an in-flight turn.
- ``SEND_ERROR``          — ``send_text_and_submit`` itself raised
  (e.g. tmux session disappeared).
"""

from __future__ import annotations

from typing import Any, Optional

from .._lifecycle.liveness_probe import (
    generate_nonce,
    pane_has_nonce_echo,
    pane_is_busy,
)
from .._state.action_base import ActionContext, PaneAction

# How much of the pane we carry forward for the completion check.
# Claude Code's TUI prints the user prompt + the response in the
# bottom ~20 lines; 4000 chars is comfortably enough to see both.
_PANE_TAIL_CHARS = 4000


class NonceProbeAction(PaneAction):
    """Functional liveness probe.

    Parameters
    ----------
    nonce:
        Optional deterministic override. Tests use this to assert
        against a known token; production callers should leave it
        ``None`` so :func:`..liveness_probe.generate_nonce` mints a
        fresh one per run.
    """

    name = "nonce-probe"

    def __init__(self, nonce: Optional[str] = None):
        self._nonce = nonce

    # ---- PaneAction surface ----------------------------------------

    def snapshot(self, ctx: ActionContext) -> dict[str, Any]:
        pane = ctx.capture_fn() or ""
        return {"pane_tail": pane[-_PANE_TAIL_CHARS:]}

    def precheck(self, before: dict[str, Any]) -> bool:
        """Refuse to probe a currently-busy pane.

        Interrupting an in-flight response with our probe would
        corrupt the user's actual work and skew quota accounting
        (the probe's ``Repeat <nonce>`` message would land as a
        new user turn while the agent was mid-reply to the prior
        turn). Defer to a later attempt; the caller can retry.
        """
        return not pane_is_busy(before.get("pane_tail", ""))

    def before_send(self, ctx: ActionContext) -> None:
        """Mint the nonce right before send so the before-snapshot's
        nonce count is guaranteed to be zero.

        Also deposits the nonce into ``ctx.extras`` so it lands in
        the attempt log's ``extras`` column — forensic readers can
        see exactly what token we asked for.
        """
        if self._nonce is None:
            self._nonce = generate_nonce()
        ctx.extras["nonce"] = self._nonce

    def send(self, ctx: ActionContext) -> None:
        assert self._nonce is not None, "before_send must mint the nonce"
        ctx.mux.send_text_and_submit(ctx.session, f"Repeat {self._nonce}")

    def is_complete(self, before: dict[str, Any], now: dict[str, Any]) -> bool:
        """``True`` iff the nonce appears at least twice in the
        post-send pane tail: once from our own ``Repeat <nonce>``
        prompt line, plus at least one more time from the agent's
        echo.
        """
        if self._nonce is None:
            # before_send never ran — probably a PRECONDITION_FAIL
            # return path that called is_complete anyway.
            return False
        return pane_has_nonce_echo(now.get("pane_tail", ""), self._nonce)
