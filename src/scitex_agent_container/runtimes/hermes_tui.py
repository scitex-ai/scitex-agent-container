"""Official Hermes Ink TUI as the owner of one SAC agent session."""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig
from ._hermes_profile import materialize_hermes_tui_profile
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
        return super().start(config, **kwargs)

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


__all__ = ["HermesTuiSessionRuntime"]
