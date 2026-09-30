"""Opencode serve gateway as the owner of one SAC agent session.

The Hermes-TUI shape at pilot scope: the inner argv runs the owner
module (serve as child + official TUI attached via ``opencode
attach``), SAC delivers turns over loopback HTTP and never types into
the terminal. Profile + env-file materialization is per-agent from
``spec.available_harnesses.opencode``; the pilot starts the
harness-neutral inbox dispatcher but no CCT poller and no recovery
monitor (those graduate with the pilot).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ._gateway_opencode import OPENCODE_GATEWAY
from ._opencode_profile import materialize_opencode_profile
from ._runtime_control import read_control_state
from .tui_session import TuiSessionRuntime, state_dir_for_config


class OpencodeTuiSessionRuntime(TuiSessionRuntime):
    """Tmux/Apptainer holder for the profile-backed opencode gateway."""

    def __init__(
        self,
        *args,
        gateway: Any | None = None,
        materialize_fn: Any | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._gateway = gateway if gateway is not None else OPENCODE_GATEWAY
        if materialize_fn is None:
            materialize_fn = materialize_opencode_profile
        self._materialize_fn = materialize_fn

    def _drain_at_boot(
        self,
        config: AgentConfig,
        *,
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> bool:
        """Wait for the owned serve gateway to publish + answer.

        Observation-only: poll ``opencode-serve.json`` + one status
        round-trip per frame. No keys are ever sent; a TUI that cannot
        bind its owner reports start failure instead of a live corpse.
        """
        from ._gateway_harness import GatewayHarnessError

        state_dir = state_dir_for_config(config)
        serve_path = Path(state_dir) / "opencode-serve.json"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if serve_path.is_file():
                try:
                    self._gateway.session_states(state_dir, timeout_s=2.0)
                    return True
                except GatewayHarnessError:
                    pass
            if poll_s > 0:
                time.sleep(poll_s)
        return False

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        """Materialize the per-agent derived opencode profile."""
        targets = self._materialize_fn(
            config, state_dir=state_dir_for_config(config)
        )
        return targets[0] if targets else None

    def start(self, config: AgentConfig, **kwargs) -> bool:
        """Start the owner+TUI session, then the inbox dispatcher."""
        kwargs["drain_pickers_at_boot"] = True
        # Not dropped: startup prompts are compiled into the incarnation
        # startup file at materialize time and submitted by the owner as
        # the session's first turns before the TUI attaches.
        kwargs["inject_startup_prompts"] = False
        started = super().start(config, **kwargs)
        if started and not kwargs.get("dry_run", False):
            try:
                self._start_inbox(config)
            except Exception:
                self._stop_inbox(config)
                super().stop(config)
                raise
        return started

    def stop(self, config: AgentConfig) -> bool:
        """Stop the inbox dispatcher, then the owned session."""
        self._stop_inbox(config)
        return super().stop(config)

    def _agent_options(self, config: AgentConfig) -> dict[str, Any]:
        return self._gateway.parse_agent_options(config)

    def send_turn(
        self, config: AgentConfig, text: str, *, wait_ready: bool = True
    ) -> bool:
        """Submit one turn through the owned serve gateway."""
        del wait_ready
        self._gateway.submit_turn(
            state_dir_for_config(config),
            config.name,
            text,
            options=self._agent_options(config),
        )
        return True

    def send_interactive_turn(
        self,
        config: AgentConfig,
        text: str,
        *,
        delivery_mode: str = "steer",
    ) -> object:
        """Return the gateway receipt for an explicitly routed inbound."""
        return self._gateway.submit_turn(
            state_dir_for_config(config),
            config.name,
            text,
            delivery_mode=delivery_mode,
            options=self._agent_options(config),
        )

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        """None when the gateway answers, else the operator-facing reason."""
        from ._gateway_harness import GatewayHarnessError

        try:
            self._gateway.session_states(state_dir_for_config(config))
            return None
        except GatewayHarnessError as exc:
            return str(exc)

    def control_state(self, config: AgentConfig) -> dict | None:
        """Runtime control plus gateway readiness for status surfaces."""
        from ._gateway_harness import GatewayHarnessError

        state_dir = state_dir_for_config(config)
        state = dict(read_control_state(state_dir) or {})
        try:
            sessions = self._gateway.session_states(state_dir)
            readiness: dict[str, Any] = {"status": "ready", "sessions": sessions}
        except GatewayHarnessError as exc:
            readiness = {"status": "unavailable", "detail": str(exc)}
        state["gateway_readiness"] = readiness
        return state


__all__ = ["OpencodeTuiSessionRuntime"]
