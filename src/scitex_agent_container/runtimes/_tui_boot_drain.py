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

Hermes boot uses its native session registry: a running startup turn is
initialized even while its input field is occupied. Input-delivery waits
retain the separate idle-input contract below.
"""

from __future__ import annotations

import logging
import time

from ..config import AgentConfig
from ._tui_drain import (
    drain_modals_until_ready,
)
from ._tui_drain import (
    wait_until_input_ready as _wait_until_input_ready,
)


def wait_for_hermes_boot(
    config,
    *,
    exists_fn,
    timeout_s,
    poll_s=0.5,
    probe_fn=None,
    poll_ui=None,
    time_fn=time.monotonic,
    sleep_fn=time.sleep,
):
    """A native working session proves boot readiness without waiting for idle.

    Optional UI polling returns True to observe the native session, False to
    refuse boot, or None after answering a modal that needs another frame.
    """
    from ._hermes_tui_rpc import HermesTuiRpcError, observe_turn_activity
    from .tui_session import state_dir_for_config

    probe = probe_fn or observe_turn_activity
    deadline = time_fn() + timeout_s
    last_error = "no initialized native session"
    while time_fn() < deadline:
        if not exists_fn():
            return False
        if poll_ui is not None:
            ui_result = poll_ui()
            if ui_result is False:
                return False
            if ui_result is None:
                sleep_fn(max(0.01, min(poll_s, deadline - time_fn())))
                continue
        try:
            activity = probe(
                state_dir_for_config(config),
                config.name,
                timeout_s=min(2.0, max(0.01, deadline - time_fn())),
            )
            if activity.session_id and activity.session_status in {
                "idle",
                "working",
                "waiting",
            }:
                return bool(exists_fn())
            last_error = f"native session status {activity.session_status!r}"
        except (HermesTuiRpcError, FileNotFoundError) as error:
            last_error = str(error)
        sleep_fn(max(0.01, min(poll_s, deadline - time_fn())))
    logging.getLogger(__name__).error(
        "Hermes boot not ready for %s: %s", config.name, last_error
    )
    return False


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
        if str(getattr(config, "harness", "") or "").lower() == "hermes":
            return wait_for_hermes_boot(
                config,
                exists_fn=lambda: self._mux.exists(name),
                timeout_s=timeout_s,
                poll_s=poll_s,
            )
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
