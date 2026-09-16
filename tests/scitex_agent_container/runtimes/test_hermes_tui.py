"""Hermes' official TUI adapter uses the shared tmux runtime boundary."""

from __future__ import annotations

from unittest.mock import patch

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._claude_spec import ClaudeSpec
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    HermesVisibleTurnReceipt,
)
from scitex_agent_container.runtimes.hermes_tui import (
    HermesTuiSessionRuntime,
    _dismiss_heartbeat_confirmation,
    _hermes_pane_is_idle,
)


class _Mux:
    def __init__(self, exists: bool = True, panes: list[str] | None = None):
        self.alive = exists
        self.panes = list(panes or [])
        self.events: list[tuple[str, str, str | None]] = []

    def exists(self, name: str) -> bool:
        self.events.append(("exists", name, None))
        return self.alive

    def send_text_literal(self, name: str, text: str) -> None:
        self.events.append(("text", name, text))

    def send_keys(self, name: str, key: str) -> None:
        self.events.append(("key", name, key))

    def send_text_and_submit(self, name: str, text: str) -> None:
        self.events.append(("settled-submit", name, text))

    def send_text_and_submit_verified(self, name: str, text: str, **kwargs) -> int:
        self.events.append(("verified-submit", name, text))
        return 1

    def capture_content(self, name: str) -> str:
        pane = self.panes.pop(0) if self.panes else ""
        self.events.append(("capture", name, pane))
        return pane


def _config() -> AgentConfig:
    return AgentConfig(name="scholar", harness="hermes", runtime="tui")


def test_fresh_session_does_not_request_continuation():
    # Arrange
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        claude=ClaudeSpec(session="fresh"),
    )
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert "--continue" not in argv and "--create-if-missing" not in argv


def test_tui_launches_through_single_gateway_owner():
    # Arrange
    config = _config()
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert argv[:9] == [
        "/usr/bin/tini",
        "-s",
        "--",
        "python3",
        "-m",
        "scitex_agent_container.runtimes._hermes_tui_owner",
        "--state-dir",
        "/state/scholar",
        "--",
    ]


def test_continue_session_resumes_the_stable_agent_session_name():
    # Arrange
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        claude=ClaudeSpec(session="continue"),
    )
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert argv[argv.index("--continue") :] == [
        "--continue",
        "sac:scholar",
        "--create-if-missing",
    ]


def test_send_turn_uses_native_rpc_for_hermes_busy_input():
    # Arrange
    mux = _Mux(
        panes=[
            "Hub is compacting...",
            "♥ Heartbeat set (every 607s): check Cards\nend · ↑/↓ scroll · Esc/q close",
        ]
    )
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: (
            calls.append((state, name, text)) or "steered"
        ),
    )
    # Act
    delivered = runtime.send_turn(
        _config(), "/heartbeat every 607s check Cards", wait_ready=False
    )
    # Assert
    assert (delivered, len(calls), calls[0][1:], mux.events) == (
        True,
        1,
        ("scholar", "/heartbeat every 607s check Cards"),
        [],
    )


def test_hermes_idle_requires_latest_ready_footer_and_empty_composer():
    # Arrange
    ready = "\n ─ ready │ qwen38 27b low │ 44% ─ sac:stats\n ❯ "
    busy = (
        "\n ─ ready │ stale footer\n ❯ \n"
        " ─ ٩(๑❛ᴗ❛๑)۶ brainstorming… · 4m │ qwen38 27b low │ 66% ─ sac:scholar\n"
        " ❯ Ctrl+C to interrupt…"
    )
    human_text = "\n ─ ready │ qwen38 27b low │ 44% ─ sac:stats\n ❯ inspect this first"
    # Act
    observed = tuple(_hermes_pane_is_idle(pane) for pane in (ready, busy, human_text))
    # Assert
    assert observed == (
        True,
        False,
        False,
    )


def test_autonomous_idle_observation_is_read_only():
    # Arrange
    mux = _Mux(panes=[" ─ brainstorming… │ qwen38 27b low │ 66% ─ sac:scholar\n ❯ "])
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: calls.append(text) or "streaming",
    )
    # Act
    idle = runtime.autonomous_control_is_idle(_config())
    # Assert
    assert (idle, [event[0] for event in mux.events]) == (
        False,
        ["exists", "capture"],
    )


def test_heartbeat_dismissal_never_escapes_arbitrary_agent_work():
    # Arrange
    mux = _Mux(
        panes=[
            "old scrollback: ♥ Heartbeat set (every 607s)\n"
            "Hub is compacting...\nesc to interrupt"
        ]
        * 3
    )
    # Act
    dismissed = _dismiss_heartbeat_confirmation(
        "tui-scholar",
        "/heartbeat every 607s check Cards",
        capture_fn=mux.capture_content,
        send_keys_fn=mux.send_keys,
        max_captures=3,
        poll_s=0,
    )
    # Assert
    assert (dismissed, [event for event in mux.events if event[0] == "key"]) == (
        False,
        [],
    )


def test_non_heartbeat_turn_does_not_probe_or_dismiss_the_pane():
    # Arrange
    mux = _Mux(
        panes=[
            "♥ Heartbeat set (every 607s): old command\nend · ↑/↓ scroll · Esc/q close"
        ]
    )
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: calls.append(text) or "streaming",
    )
    # Act
    delivered = runtime.send_turn(_config(), "new guidance", wait_ready=False)
    # Assert
    assert (delivered, calls, mux.events) == (True, ["new guidance"], [])


def test_send_key_refuses_when_tmux_session_is_absent():
    # Arrange
    runtime = HermesTuiSessionRuntime(multiplexer=_Mux(exists=False))
    # Act
    delivered = runtime.send_key(_config(), "Enter")
    # Assert
    assert delivered is False


def test_deliverability_requires_authenticated_gateway_readiness():
    # Arrange
    def degraded(_state):
        raise HermesTuiRpcError("Hermes authenticated readiness is degraded")

    runtime = HermesTuiSessionRuntime(
        multiplexer=_Mux(), gateway_health=degraded
    )

    # Act
    reason = runtime.why_not_deliverable(_config())

    # Assert
    assert reason == "Hermes authenticated readiness is degraded"


def test_control_state_surfaces_authenticated_readiness_json():
    # Arrange
    readiness = {
        "status": "ok",
        "readiness": {"checks": [{"name": "disk", "ok": True}]},
    }
    runtime = HermesTuiSessionRuntime(
        multiplexer=_Mux(),
        control_state_reader=lambda _state: {"turn_admission": "ready"},
        gateway_health=lambda _state: readiness,
    )

    # Act
    state = runtime.control_state(_config())

    # Assert
    assert state == {
        "turn_admission": "ready",
        "gateway_readiness": readiness,
    }


def test_visible_incoming_turn_uses_only_native_hermes_rpc():
    # Arrange
    mux = _Mux(panes=["❯ operator draft that must remain untouched"])
    calls = []
    receipt = HermesVisibleTurnReceipt(
        status="steered",
        visibility="session.inflight.corrections",
        session_id="live-1",
    )

    def submit_visible(state, name, text, **kwargs):
        calls.append((state, name, text, kwargs))
        return receipt

    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit_visible=submit_visible,
    )
    text = "<channel>digest</channel><!-- delivery:n_native -->"

    # Act
    delivered = runtime.send_visible_turn(
        _config(),
        text,
        visible_delivery_id="n_native",
        max_observations=7,
        poll_s=0.25,
    )

    # Assert
    assert (delivered, calls[0][1:], mux.events) == (
        receipt,
        (
            "scholar",
            text,
            {
                "delivery_id": "n_native",
                "delivery_mode": "steer",
                "max_observations": 7,
                "poll_s": 0.25,
            },
        ),
        [],
    )


class _CctLifecycleRuntime(HermesTuiSessionRuntime):
    def __init__(self, *, fail_at: str = ""):
        super().__init__(multiplexer=_Mux())
        self.events: list[str] = []
        self.fail_at = fail_at

    def _event(self, value: str):
        self.events.append(value)
        if self.fail_at == value:
            raise RuntimeError(value)

    def _start_session(self, config, **kwargs):
        self._event("session:start")
        return True

    def _stop_session(self, config):
        self._event("session:stop")
        return True

    def _start_cct(self, config):
        self._event("cct:start")

    def _stop_cct(self, config):
        self._event("cct:stop")

    def _start_inbox(self, config):
        self._event("inbox:start")

    def _stop_inbox(self, config):
        self._event("inbox:stop")

    def _start_recovery(self, config):
        self._event("recovery:start")

    def _stop_recovery(self, config):
        self._event("recovery:stop")


def test_hermes_starts_poller_after_turn_bridge_session_and_stops_it_first():
    # Arrange
    runtime = _CctLifecycleRuntime()
    # Act
    started = runtime.start(_config())
    stopped = runtime.stop(_config())
    # Assert
    assert (started, stopped, runtime.events) == (
        True,
        True,
        [
            "session:start",
            "cct:start",
            "inbox:start",
            "recovery:start",
            "cct:stop",
            "inbox:stop",
            "recovery:stop",
            "session:stop",
        ],
    )


def test_hermes_auxiliary_failure_cleans_poller_before_session():
    # Arrange
    runtime = _CctLifecycleRuntime(fail_at="inbox:start")
    # Act
    try:
        runtime.start(_config())
    except RuntimeError as exc:
        error = str(exc)
    # Assert
    assert (error, runtime.events) == (
        "inbox:start",
        [
            "session:start",
            "cct:start",
            "inbox:start",
            "cct:stop",
            "inbox:stop",
            "recovery:stop",
            "session:stop",
        ],
    )


def test_recovery_uses_supported_same_session_controls_in_order():
    # Arrange
    config = _config()
    config.model = "qwen38-27b"
    config.engine_key = "qwen38-27b"
    mux = _Mux()
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
    )
    runtime.disable_periodic_turns = lambda _config: (
        calls.append("heartbeat.clear") or True
    )
    # Act
    disabled = runtime.disable_periodic_turns(config)
    with patch(
        "scitex_agent_container.runtimes._hermes_tui_rpc.execute_slash_command",
        side_effect=lambda state, name, command: calls.append(command) or "switched",
    ):
        recovered = runtime.recover_turn_admission(config)
    # Assert
    assert (
        disabled,
        recovered,
        calls,
    ) == (
        True,
        True,
        [
            "heartbeat.clear",
            "/model qwen38-27b --provider custom:sac-qwen38-27b --session",
        ],
    )


class _LifecycleRuntime(HermesTuiSessionRuntime):
    def __init__(self):
        super().__init__(multiplexer=_Mux())
        self.lifecycle_events = []

    def _start_session(self, _config, **_kwargs):
        self.lifecycle_events.append("tmux-start")
        return True

    def _stop_session(self, _config):
        self.lifecycle_events.append("tmux-stop")
        return True

    def _start_cct(self, _config):
        self.lifecycle_events.append("cct-start")

    def _stop_cct(self, _config):
        self.lifecycle_events.append("cct-stop")

    def _start_inbox(self, _config):
        self.lifecycle_events.append("inbox-start")

    def _stop_inbox(self, _config):
        self.lifecycle_events.append("inbox-stop")

    def _start_recovery(self, _config):
        self.lifecycle_events.append("recovery-start")

    def _stop_recovery(self, _config):
        self.lifecycle_events.append("recovery-stop")


def test_start_attaches_authenticated_inbox_bridge_before_recovery():
    # Arrange
    runtime = _LifecycleRuntime()
    # Act
    started = runtime.start(_config())
    # Assert
    assert (started, runtime.lifecycle_events) == (
        True,
        ["tmux-start", "cct-start", "inbox-start", "recovery-start"],
    )


def test_stop_detaches_inbox_before_tmux_session():
    # Arrange
    runtime = _LifecycleRuntime()
    # Act
    stopped = runtime.stop(_config())
    # Assert
    assert (stopped, runtime.lifecycle_events) == (
        True,
        ["cct-stop", "inbox-stop", "recovery-stop", "tmux-stop"],
    )
