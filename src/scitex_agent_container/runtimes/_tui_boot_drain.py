"""Boot-drain / input-readiness mixin for the TUI runtime.

Extracted from :mod:`runtimes.tui_session` (512-line per-file cap) so the
boot-readiness group has room to live beside the pure helpers it delegates
to. Mirrors the mixin convention already used by this runtime
(:class:`_tui_inject.StartupPromptInjectorMixin`,
:class:`_tui_bridge_seam.TurnBridgeSeamMixin`).

ONE responsibility: drive a freshly-launched TUI through claude's first-run
modals (bypass-permissions / trust / theme) until its input field is bound.
Every method here is a thin, session-aware wrapper over the pure,
unit-testable functions in :mod:`_tui_drain` — the mixin supplies the
``self._mux`` collaborators and the ``tui-<name>`` session name; the rules
(fail-fast-on-session-death, settle-before-send, verified-resend, dismiss by
REGISTERED keys and never Escape) live in ``_tui_drain`` and are unchanged.

Behaviour is IDENTICAL to the pre-extraction methods — this is a pure move.
"""

from __future__ import annotations

import time

from ..config import AgentConfig
from ._tui_drain import (
    drain_modals_until_ready,
    wait_until_input_ready as _wait_until_input_ready,
)


def _session_name(config: AgentConfig) -> str:
    """Resolve the agent's ``tui-<name>`` session.

    Local import of :func:`tui_session.session_name_for` keeps the session
    naming on its one canonical implementation without a module-level import
    cycle (``tui_session`` imports this mixin). Same pattern as
    :func:`_runners._tmux._tmux_probe._display_field`.
    """
    from .tui_session import session_name_for

    return session_name_for(config)


class TuiBootDrainMixin:
    """Modal-drain + input-readiness methods for :class:`TuiSessionRuntime`.

    Expects the host class to provide ``self._mux`` (a ``TmuxManager``-shaped
    multiplexer exposing ``exists`` / ``capture_content`` / ``send_keys``).
    """

    def _drain_at_boot(
        self,
        config: AgentConfig,
        *,
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> bool:
        """Dismiss claude's first-run modals at boot; return as soon as the TUI
        is up (marker OR :func:`prompts.is_ready`) — not when it goes idle, so
        an autonomous agent that goes straight to work is not waited out, and a
        ``startup_commands``-delayed ``exec claude`` is polled through. Thin
        wrapper over :meth:`_drain_modals_until_ready`. Best-effort: never
        raises. Returns True iff a ready signal was observed within the window.
        """
        name = _session_name(config)
        if not self._mux.exists(name):
            return False
        return self._drain_modals_until_ready(name, timeout_s=timeout_s, poll_s=poll_s)

    def _drain_modals_until_ready(
        self,
        name: str,
        *,
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> bool:
        """Verified, retrying, fail-loud modal drain. True iff ready in window.

        Thin wrapper over the pure, unit-testable
        :func:`_tui_drain.drain_modals_until_ready` (fail-fast-on-session-death,
        settle-before-send [BUG 2], verified-resend). Dismisses modals by their
        REGISTERED keys (Enter/digit, never Escape), so a dev-channels
        "Esc to cancel" modal is CONFIRMED — the session-killing Escape lives
        only in the guarded compose-buffer clear (BUG 1).
        """
        return drain_modals_until_ready(
            name,
            capture_fn=self._mux.capture_content,
            send_keys_fn=lambda key: self._mux.send_keys(name, key),
            exists_fn=self._mux.exists,
            timeout_s=timeout_s,
            poll_s=poll_s,
        )

    def wait_until_input_ready(
        self,
        config: AgentConfig,
        *,
        timeout_s: float = 60.0,
        poll_s: float = 0.4,
        sleep_fn=time.sleep,
    ) -> bool:
        """Drain first-launch / mid-session modals, then block until the TUI
        input field is bound.

        Thin wrapper over the pure, unit-testable
        :func:`_tui_drain.wait_until_input_ready`: dismisses each modal by its
        REGISTERED keys (never Escape → dev-channels is CONFIRMED, BUG 1) and
        SETTLES the pane before sending (BUG 2). Raises
        :class:`TuiInputNotReadyError` on timeout.
        """
        del sleep_fn  # honoured internally by the extracted function's default
        name = _session_name(config)
        return _wait_until_input_ready(
            name,
            capture_fn=self._mux.capture_content,
            send_keys_fn=lambda key: self._mux.send_keys(name, key),
            exists_fn=self._mux.exists,
            timeout_s=timeout_s,
            poll_s=poll_s,
        )


__all__ = ["TuiBootDrainMixin"]
