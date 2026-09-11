"""Official Hermes Ink TUI as the owner of one SAC agent session."""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig
from ._hermes_profile import materialize_hermes_tui_profile
from ._runtime_control import read_control_state
from .tui_session import TuiSessionRuntime, state_dir_for_config


class HermesTuiSessionRuntime(TuiSessionRuntime):
    """Tmux/Apptainer holder for the profile-backed Hermes TUI."""

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        targets = materialize_hermes_tui_profile(
            config, state_dir=state_dir_for_config(config)
        )
        return targets[0] if targets else None

    def start(self, config: AgentConfig, **kwargs) -> bool:
        # Claude's modal drainer does not understand Hermes' screen, and the
        # Hermes argv already carries the startup prompts as its first query.
        kwargs["drain_pickers_at_boot"] = False
        kwargs["inject_startup_prompts"] = False
        started = super().start(config, **kwargs)
        if started and not kwargs.get("dry_run", False):
            from ._hermes_stale_recovery import start_recovery_monitor

            start_recovery_monitor(config)
        return started

    def stop(self, config: AgentConfig) -> bool:
        from ._hermes_stale_recovery import stop_recovery_monitor

        stop_recovery_monitor(config)
        return super().stop(config)

    def send_turn(
        self, config: AgentConfig, text: str, *, wait_ready: bool = True
    ) -> bool:
        """Paste through Hermes' native busy-input queue."""
        del wait_ready
        name = self.session_name(config)
        if not name or not self._mux.exists(name):
            return False
        self._mux.send_text_literal(name, text)
        self._mux.send_keys(name, "Enter")
        return True

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        name = self.session_name(config)
        if name and self._mux.exists(name):
            return None
        return "the Hermes TUI tmux session is absent"

    def control_state(self, config: AgentConfig) -> dict | None:
        return read_control_state(state_dir_for_config(config))

    def recover_turn_admission(self, config: AgentConfig) -> bool:
        """Use Hermes' supported same-session model switch, then resume wakeups."""
        from ._hermes_stale_recovery import recovery_command

        rebound = self.send_turn(config, recovery_command(config), wait_ready=False)
        if not rebound:
            return False
        return self.send_turn(config, "/heartbeat resume", wait_ready=False)

    def suspend_autonomous_turns(self, config: AgentConfig) -> bool:
        """Pause Hermes' native scheduler while its provider is stale-latched."""
        return self.send_turn(config, "/heartbeat pause", wait_ready=False)


__all__ = ["HermesTuiSessionRuntime"]
