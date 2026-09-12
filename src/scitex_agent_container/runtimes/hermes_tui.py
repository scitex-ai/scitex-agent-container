"""Official Hermes Ink TUI as the owner of one SAC agent session."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from ..config import AgentConfig
from ._hermes_profile import materialize_hermes_tui_profile
from ._runtime_control import read_control_state
from ._tui_compose import _compose_pending_live, composer_holds_fragment
from .tui_session import TuiSessionRuntime, state_dir_for_config

_HEARTBEAT_SET_COMMAND = "/heartbeat every "
_HEARTBEAT_SET_CONFIRMATION = "heartbeat set (every "
_HEARTBEAT_CONFIRMATION_CLOSE = "esc/q close"
_HERMES_STATUS_RE = re.compile(r"(?m)^[ \t]*─+\s+([^\u2502\n]+?)\s*\u2502")
_HERMES_COMPOSER_RE = re.compile(r"(?m)^[ \t]*❯(?P<body>[^\n]*)$")
_WS_RE = re.compile(r"[\s\xa0]+")


def _live_composer_body(pane: str) -> str | None:
    """Return the whitespace-free live Hermes composer, or ``None``.

    Hermes hard-wraps a long paste across terminal rows, so the body is the
    text after the bottom-most composer marker plus every following row.
    Whitespace is presentation-only here; removing it lets a raw payload and
    its wrapped rendering compare without guessing the terminal width.
    """
    rows = (pane or "").splitlines()
    for index in range(len(rows) - 1, -1, -1):
        marker = rows[index].find("❯")
        if marker >= 0:
            body = rows[index][marker + 1 :] + "".join(rows[index + 1 :])
            return _WS_RE.sub("", body)
    return None


def _delivery_copies_in_composer(
    pane: str, *, text: str, delivery_id: str
) -> int | None:
    """Count copies of exactly one delivery in the live composer.

    Returns ``None`` if anything besides copies of ``text`` (raw rendering) or
    Hermes' collapsed paste chip for ``delivery_id`` is present.  That refusal
    is load-bearing: SAC may normalize its own duplicate retry, but it must
    never clear or submit unrelated operator text.
    """
    body = _live_composer_body(pane)
    if not body:
        return None

    raw = _WS_RE.sub("", text)
    if raw and len(body) % len(raw) == 0 and body == raw * (len(body) // len(raw)):
        return len(body) // len(raw)

    escaped_id = re.escape(delivery_id)
    collapsed = re.compile(
        rf"(?:\[\[<channelsource=\.\.\[\d+lines\]\.\."
        rf"delivery:{escaped_id}-->\]\])+"
    )
    if collapsed.fullmatch(body):
        return len(re.findall(rf"delivery:{escaped_id}-->", body))
    return None


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

    def __init__(self, *args, rpc_submit: Callable[..., str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if rpc_submit is None:
            from ._hermes_tui_rpc import submit_turn

            rpc_submit = submit_turn
        self._rpc_submit = rpc_submit

    def _start_session(self, config: AgentConfig, **kwargs) -> bool:
        return super().start(config, **kwargs)

    def _stop_session(self, config: AgentConfig) -> bool:
        return super().stop(config)

    @staticmethod
    def _start_inbox(config: AgentConfig) -> None:
        from ._hermes_inbox_bridge_lifecycle import start_inbox_bridge

        start_inbox_bridge(config)

    @staticmethod
    def _stop_inbox(config: AgentConfig) -> None:
        from ._hermes_inbox_bridge_lifecycle import stop_inbox_bridge

        stop_inbox_bridge(config)

    @staticmethod
    def _start_recovery(config: AgentConfig) -> None:
        from ._hermes_stale_recovery import start_recovery_monitor

        start_recovery_monitor(config)

    @staticmethod
    def _stop_recovery(config: AgentConfig) -> None:
        from ._hermes_stale_recovery import stop_recovery_monitor

        stop_recovery_monitor(config)

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
        started = self._start_session(config, **kwargs)
        if started and not kwargs.get("dry_run", False):
            try:
                self._start_inbox(config)
            except Exception:
                self._stop_session(config)
                raise
            self._start_recovery(config)
        return started

    def stop(self, config: AgentConfig) -> bool:
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
        max_captures: int = 20,
        poll_s: float = 0.1,
    ) -> bool:
        """Submit one durable inbound and prove it is in Hermes' transcript.

        Hermes has no server-to-TUI notification API equivalent to Claude
        Code's ``notifications/claude/channel``.  Its supported TUI ingress is
        the composer; ``display.busy_input_mode=steer`` makes Enter route busy
        input at the next safe boundary.  Therefore SAC verifies both halves:
        the literal text echoed before Enter, then its unique delivery id is
        visible outside the live composer.  A staged human composer is never
        overwritten.  False means the durable caller must not acknowledge.
        """
        name = self.session_name(config)
        if not name or not self._mux.exists(name):
            return False
        initial = self._mux.capture_content(name)
        marker_seen = visible_delivery_id in initial
        marker_staged = marker_seen and composer_holds_fragment(
            initial, visible_delivery_id
        )
        if marker_seen and not marker_staged:
            # A prior delivery reached the transcript but its downstream Cards
            # ACK did not. Re-prove the same observation without injecting a
            # duplicate turn, then let the durable poller retry only the ACK.
            return True
        copies = None
        if marker_staged:
            copies = _delivery_copies_in_composer(
                initial,
                text=text,
                delivery_id=visible_delivery_id,
            )
            if copies is None:
                # The marker is mixed with text SAC cannot prove it owns.
                # Never clear or submit a human's staged input.
                return False
            if copies > 1 and not self._clear_compose_buffer(name):
                return False
        elif _compose_pending_live(initial):
            return False

        # Paste ONCE.  Hermes renders multiline paste as a collapsed chip, so
        # the generic tmux helper's literal-prefix echo check cannot see it and
        # used to paste the same envelope four times before giving up.  The
        # durable delivery marker survives that collapse and is the correct
        # observation seam.  On retry, one already-staged copy is submitted as
        # is; multiple proven-identical copies are cleared and normalized to
        # one.  No retry ever appends another copy.
        if copies != 1:
            self._mux.send_text_literal(name, text)
        if not self._verify_submitted(
            name,
            pasted=visible_delivery_id,
            max_resends=4,
            poll_s=poll_s,
            appear_timeout_s=max(2.0, max_captures * poll_s),
            idle_wait_s=30.0,
        ):
            return False

        for attempt in range(max_captures):
            pane = self._mux.capture_content(name)
            marker_seen = visible_delivery_id in pane
            still_staged = composer_holds_fragment(pane, visible_delivery_id)
            if marker_seen and not still_staged:
                return True
            if poll_s > 0 and attempt + 1 < max_captures:
                time.sleep(poll_s)
        return False

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        from ._hermes_tui_owner import GATEWAY_FILE

        if (state_dir_for_config(config) / GATEWAY_FILE).is_file():
            return None
        return "the Hermes native TUI gateway is absent"

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
