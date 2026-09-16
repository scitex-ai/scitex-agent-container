"""Official Hermes Ink TUI as the owner of one SAC agent session."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from ..config import AgentConfig
from ._hermes_profile import materialize_hermes_tui_profile
from ._runtime_control import read_control_state
from .tui_session import TuiSessionRuntime, state_dir_for_config

_HEARTBEAT_SET_COMMAND = "/heartbeat every "
_HEARTBEAT_SET_CONFIRMATION = "heartbeat set (every "
_HEARTBEAT_CONFIRMATION_CLOSE = "esc/q close"
_HERMES_STATUS_RE = re.compile(r"(?m)^[ \t]*─+\s+([^\u2502\n]+?)\s*\u2502")
_HERMES_COMPOSER_RE = re.compile(r"(?m)^[ \t]*❯(?P<body>[^\n]*)$")


def _hermes_pane_boot_ready(pane: str) -> bool | None:
    """Classify a bound Hermes composer, rejecting its setup wall."""
    statuses = list(_HERMES_STATUS_RE.finditer(pane or ""))
    composers = list(_HERMES_COMPOSER_RE.finditer(pane or ""))
    if not statuses or not composers:
        return None
    status = statuses[-1]
    composer = composers[-1]
    if composer.start() <= status.end():
        return None
    return "setup required" not in status.group(1).strip().lower()


def _hermes_pane_is_idle(pane: str) -> bool:
    """True only for Hermes' live ``ready`` footer and empty composer.

    Choosing the last status and composer rows excludes stale ``ready`` text
    in scrollback.  Requiring both signals also preserves text a human has
    already staged: an empty-looking status alone never authorizes SAC to
    write into the shared composer.
    """
    statuses = list(_HERMES_STATUS_RE.finditer(pane or ""))
    composers = list(_HERMES_COMPOSER_RE.finditer(pane or ""))
    if not statuses or not composers:
        return False
    status = statuses[-1]
    composer = composers[-1]
    return (
        status.group(1).strip().lower() == "ready"
        and composer.start() > status.end()
        and not composer.group("body").strip()
    )


def _dismiss_heartbeat_confirmation(
    name: str,
    command: str,
    *,
    capture_fn: Callable[[str], str],
    send_keys_fn: Callable[[str, str], None],
    max_captures: int = 50,
    poll_s: float = 0.1,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Close only the confirmation opened by a heartbeat-set command.

    Hermes renders slash-command output asynchronously after accepting Enter.
    The long autonomous heartbeat confirmation opens a focused scroll view, so
    wait for that exact view before sending Escape.  A generic busy pane, an
    agent approval, and every non-heartbeat turn are deliberately untouched.
    """
    if not command.strip().lower().startswith(_HEARTBEAT_SET_COMMAND):
        return False
    for attempt in range(max_captures):
        pane = capture_fn(name).lower()
        if (
            _HEARTBEAT_SET_CONFIRMATION in pane
            and _HEARTBEAT_CONFIRMATION_CLOSE in pane
        ):
            send_keys_fn(name, "Escape")
            return True
        if poll_s > 0 and attempt + 1 < max_captures:
            sleep_fn(poll_s)
    return False


class HermesTuiSessionRuntime(TuiSessionRuntime):
    """Tmux/Apptainer holder for the profile-backed Hermes TUI."""

    def __init__(
        self,
        *args,
        rpc_submit: Callable[..., object] | None = None,
        rpc_submit_visible: Callable[..., object] | None = None,
        gateway_health: Callable[[Path], dict] | None = None,
        control_state_reader: Callable[[Path], dict | None] = read_control_state,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if rpc_submit is None:
            from ._hermes_tui_rpc import submit_turn

            rpc_submit = submit_turn
        if rpc_submit_visible is None:
            from ._hermes_tui_rpc import submit_visible_turn

            rpc_submit_visible = submit_visible_turn
        self._rpc_submit = rpc_submit
        self._rpc_submit_visible = rpc_submit_visible
        self._gateway_health = gateway_health
        self._control_state_reader = control_state_reader

    def _read_gateway_health(self, state_dir: Path) -> dict:
        if self._gateway_health is not None:
            return self._gateway_health(state_dir)
        from ._hermes_tui_rpc import gateway_detailed_health

        return gateway_detailed_health(state_dir)

    def _start_session(self, config: AgentConfig, **kwargs) -> bool:
        return super().start(config, **kwargs)

    def _stop_session(self, config: AgentConfig) -> bool:
        return super().stop(config)

    def _drain_at_boot(
        self,
        config: AgentConfig,
        *,
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> bool:
        """Observe Hermes' own footer until its session composer is bound."""
        import logging

        name = self.session_name(config)
        deadline = time.monotonic() + timeout_s
        while name and self._mux.exists(name) and time.monotonic() < deadline:
            pane = self._mux.capture_content(name)
            ready = _hermes_pane_boot_ready(pane)
            if ready is not None:
                if not ready:
                    logging.getLogger(__name__).error(
                        "Hermes TUI start refused for %s: pane is at Setup Required "
                        "and has no active model session",
                        config.name,
                    )
                return ready
            if poll_s > 0:
                time.sleep(poll_s)
        return False

    @staticmethod
    def _start_recovery(config: AgentConfig) -> None:
        from ._hermes_stale_recovery import start_recovery_monitor

        start_recovery_monitor(config)

    @staticmethod
    def _stop_recovery(config: AgentConfig) -> None:
        from ._hermes_stale_recovery import stop_recovery_monitor

        stop_recovery_monitor(config)

    @staticmethod
    def _start_cct(config: AgentConfig) -> None:
        from ._tui_cct_poller import start_cct_poller

        start_cct_poller(config)

    @staticmethod
    def _stop_cct(config: AgentConfig) -> None:
        from ._tui_cct_poller import stop_cct_poller

        stop_cct_poller(config)

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        targets = materialize_hermes_tui_profile(
            config, state_dir=state_dir_for_config(config)
        )
        return targets[0] if targets else None

    def start(self, config: AgentConfig, **kwargs) -> bool:
        # Use Hermes' observation-only boot drain above. It sends no Claude
        # picker keys and rejects the live-but-sessionless Setup Required wall.
        kwargs["drain_pickers_at_boot"] = True
        kwargs["inject_startup_prompts"] = False
        started = self._start_session(config, **kwargs)
        if started and not kwargs.get("dry_run", False):
            try:
                self._start_cct(config)
                self._start_inbox(config)
                self._start_recovery(config)
            except Exception:
                self._stop_cct(config)
                self._stop_inbox(config)
                self._stop_recovery(config)
                self._stop_session(config)
                raise
        return started

    def stop(self, config: AgentConfig) -> bool:
        self._stop_cct(config)
        self._stop_inbox(config)
        self._stop_recovery(config)
        return self._stop_session(config)

    def send_turn(
        self, config: AgentConfig, text: str, *, wait_ready: bool = True
    ) -> bool:
        """Submit through Hermes' native busy-input routing.

        SAC-generated Hermes profiles default that routing to ``steer``:
        input arriving during a live turn is injected at the next safe tool
        boundary, while idle input remains an ordinary new turn.
        """
        del wait_ready
        self._rpc_submit(state_dir_for_config(config), config.name, text)
        return True

    def send_interactive_turn(
        self,
        config: AgentConfig,
        text: str,
        *,
        delivery_mode: str = "steer",
    ) -> object:
        """Return Hermes' native receipt for an explicitly routed inbound."""
        return self._rpc_submit(
            state_dir_for_config(config),
            config.name,
            text,
            delivery_mode=delivery_mode,
        )

    def send_key(self, config: AgentConfig, key: str) -> bool:
        """Send an explicit UI-control key; prompts never use this path."""
        return super().send_key(config, key)

    def autonomous_control_is_idle(self, config: AgentConfig) -> bool:
        """Observe whether SAC may safely use Hermes' shared composer now."""
        name = self.session_name(config)
        return bool(
            name
            and self._mux.exists(name)
            and _hermes_pane_is_idle(self._mux.capture_content(name))
        )

    def send_visible_turn(
        self,
        config: AgentConfig,
        text: str,
        *,
        visible_delivery_id: str,
        delivery_mode: str = "steer",
        max_observations: int = 20,
        poll_s: float = 0.1,
    ) -> object:
        """Submit durably through Hermes and prove its native user projection.

        Message delivery never mutates the tmux PTY. ``prompt.submit`` applies
        the generated profile's ``busy_input_mode=steer`` policy, and the
        returned receipt is issued only after ``session.activate`` shows the
        unique delivery marker in messages, inflight corrections, or Hermes'
        native queue.  Any exception keeps the durable caller unconfirmed.
        """
        return self._rpc_submit_visible(
            state_dir_for_config(config),
            config.name,
            text,
            delivery_id=visible_delivery_id,
            delivery_mode=delivery_mode,
            max_observations=max_observations,
            poll_s=poll_s,
        )

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        from ._hermes_tui_rpc import HermesTuiRpcError

        try:
            self._read_gateway_health(state_dir_for_config(config))
            return None
        except HermesTuiRpcError as exc:
            return str(exc)

    def control_state(self, config: AgentConfig) -> dict | None:
        from ._hermes_tui_rpc import HermesTuiRpcError

        state_dir = state_dir_for_config(config)
        state = dict(self._control_state_reader(state_dir) or {})
        try:
            readiness = self._read_gateway_health(state_dir)
        except HermesTuiRpcError as exc:
            readiness = {"status": "unavailable", "detail": str(exc)}
        state["gateway_readiness"] = readiness
        return state

    def recover_turn_admission(self, config: AgentConfig) -> bool:
        """Use Hermes' supported same-session model switch."""
        from ._hermes_stale_recovery import recovery_command

        return self.send_turn(config, recovery_command(config), wait_ready=False)

    def disable_periodic_turns(self, config: AgentConfig) -> bool:
        """Remove model-calling heartbeat state through Hermes' control plane."""
        from ._hermes_tui_rpc import clear_heartbeat

        return clear_heartbeat(
            state_dir_for_config(config), config.name
        ) == "absent"

__all__ = ["HermesTuiSessionRuntime"]
