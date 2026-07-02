"""Startup-prompt injection mixin for the TUI runtime.

Extracted from :mod:`tui_session` to keep that module under the line limit.
:class:`StartupPromptInjectorMixin` groups the boot-time prompt-injection
behaviour: drain modals to input-ready, clear any stale compose buffer, then
paste + submit each ``spec.startup_prompts`` entry with verified submission.

The compose-buffer clear and submit-verification are thin wrappers over the
pure, injectable primitives in :mod:`_tui_compose` (so the algorithms stay
unit-testable without a live TUI); this mixin owns only the orchestration.
"""

from __future__ import annotations

import logging

from ..config import AgentConfig
from ._tui_compose import clear_compose_buffer, verify_submit_by_advancement

__all__ = ["StartupPromptInjectorMixin"]


class StartupPromptInjectorMixin:
    """Boot-time startup-prompt injection for :class:`TuiSessionRuntime`.

    Expects the host class to provide ``self._mux`` (a MultiplexerProtocol),
    ``self.wait_until_input_ready(config)`` (the modal drain), and a
    module-level ``session_name_for`` — all satisfied by
    :class:`TuiSessionRuntime`.
    """

    def _inject_startup_prompts(self, config: AgentConfig) -> None:
        """Feed spec.startup_prompts as the first user turn(s).

        Each prompt = a separate turn via ``send_text_and_submit``, gated on
        ``wait_until_input_ready`` BEFORE the send and followed by a defensive
        trailing ``Enter``. Empty list → no-op. Per-prompt failure logged and
        skipped; total failure does NOT raise so the supervisor restart cycle
        never oscillates.

        BUG 1 reorder (card sac-boot-automation-devchannels-...): the
        compose-buffer clear sends ``Escape`` — which CANCELS a still-open
        dev-channels / "Esc to cancel" modal and KILLS the session. So drain
        modals to input-ready FIRST (this dismisses dev-channels via Enter →
        option 1); the Escape-based clear below only runs once NO cancelable
        modal remains on screen, and the clear itself REFUSES to Esc while such
        a modal is present (belt-and-suspenders — see :func:`clear_compose_buffer`).

        Stale-compose fix (card sac-tui-clear-compose-buffer-on-boot): a
        persistent tmux pane carries EXTERNAL pasted-but-unsent text across a
        restart (e.g. a burst of stale ``/compact``); clearing before the first
        submit stops the boot Enter from submitting that stale stack. P0
        Enter-drop fix: gate each send on readiness + append a defensive Enter.
        """
        from .tui_session import session_name_for

        log = logging.getLogger(__name__)
        prompts = list(getattr(config, "startup_prompts", []) or [])
        if not prompts:
            return
        name = session_name_for(config)
        # Short pre-clear drain: the boot-drain already spent the long window;
        # this only needs to CONFIRM no cancelable modal remains before the
        # Escape-based clear. A short timeout keeps a not-ready pane from
        # doubling the wait (the per-prompt gate below owns the real patience).
        try:
            self.wait_until_input_ready(config, timeout_s=5.0)
        except Exception as exc:  # stx-allow: fallback (reason: a drain timeout here must not skip prompt injection outright — the compose-clear Esc-guard + per-prompt wait_until_input_ready are the downstream nets; logged LOUD)
            log.warning(
                "TuiSessionRuntime: pre-clear modal drain for %s did not reach "
                "input-ready (%s); proceeding — the compose-clear Esc-guard and "
                "per-prompt readiness wait are the downstream nets.",
                name,
                exc,
            )
        self._clear_compose_buffer(name)
        for index, prompt in enumerate(prompts, start=1):
            if not prompt:
                continue
            try:
                self.wait_until_input_ready(config)
                self._mux.send_text_and_submit(name, prompt)
                self._mux.send_keys(name, "Enter")
                self._verify_submitted(name)
                log.info(
                    "TuiSessionRuntime: injected startup_prompt %d/%d "
                    "(%d chars) into %s (with defensive Enter)",
                    index,
                    len(prompts),
                    len(prompt),
                    name,
                )
            except Exception as exc:  # stx-allow: fallback (per-prompt best-effort)
                log.warning(
                    "TuiSessionRuntime: startup_prompt %d/%d failed for %s: %s",
                    index,
                    len(prompts),
                    name,
                    exc,
                )

    def _clear_compose_buffer(
        self,
        name: str,
        *,
        max_attempts: int = 5,
        poll_s: float = 0.4,
    ) -> bool:
        """Clear stale compose-pending text before the boot submit.

        Thin wrapper over the pure, unit-testable :func:`clear_compose_buffer`
        (no-op-when-empty / double-Escape clear, with the BUG 1 Esc-cancel
        guard). Refuses to Esc while a cancelable modal is on screen.
        """
        return clear_compose_buffer(
            name,
            capture_fn=self._mux.capture_content,
            send_keys_fn=lambda key: self._mux.send_keys(name, key),
            max_attempts=max_attempts,
            poll_s=poll_s,
        )

    def _verify_submitted(
        self,
        name: str,
        *,
        max_resends: int = 8,
        poll_s: float = 0.6,
        appear_timeout_s: float = 5.0,
        idle_wait_s: float = 30.0,
    ) -> bool:
        """Verify a just-pasted turn was SUBMITTED; resend Enter once idle.

        Thin wrapper over the pure, unit-testable
        :func:`verify_submit_by_advancement` (wait-for-idle + verify-by-
        advancement — the boot Enter-drop fix, sac-tui-enter-drop-on-boot).
        """
        return verify_submit_by_advancement(
            name,
            capture_fn=self._mux.capture_content,
            send_keys_fn=lambda key: self._mux.send_keys(name, key),
            max_resends=max_resends,
            poll_s=poll_s,
            appear_timeout_s=appear_timeout_s,
            idle_wait_s=idle_wait_s,
        )
