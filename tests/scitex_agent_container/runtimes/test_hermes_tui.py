"""Hermes' official TUI adapter uses the shared tmux runtime boundary."""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._claude_spec import ClaudeSpec
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
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


def test_send_turn_uses_settled_submit_for_hermes_busy_input():
    # Arrange
    mux = _Mux(
        panes=[
            "Hub is compacting...",
            "♥ Heartbeat set (every 607s): check Cards\n"
            "end · ↑/↓ scroll · Esc/q close",
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_turn(
        _config(), "/heartbeat every 607s check Cards", wait_ready=False
    )
    # Assert
    assert (delivered, mux.events) == (
        True,
        [
            ("exists", "tui-scholar", None),
            (
                "settled-submit",
                "tui-scholar",
                "/heartbeat every 607s check Cards",
            ),
            ("capture", "tui-scholar", "Hub is compacting..."),
            (
                "capture",
                "tui-scholar",
                "♥ Heartbeat set (every 607s): check Cards\n"
                "end · ↑/↓ scroll · Esc/q close",
            ),
            ("key", "tui-scholar", "Escape"),
        ],
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
    observed = tuple(
        _hermes_pane_is_idle(pane) for pane in (ready, busy, human_text)
    )
    # Assert
    assert observed == (
        True,
        False,
        False,
    )


def test_autonomous_idle_observation_is_read_only():
    # Arrange
    mux = _Mux(
        panes=[
            " ─ brainstorming… │ qwen38 27b low │ 66% ─ sac:scholar\n ❯ "
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
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
            "♥ Heartbeat set (every 607s): old command\n"
            "end · ↑/↓ scroll · Esc/q close"
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_turn(_config(), "new guidance", wait_ready=False)
    # Assert
    assert (delivered, mux.events) == (
        True,
        [
            ("exists", "tui-scholar", None),
            ("settled-submit", "tui-scholar", "new guidance"),
        ],
    )


def test_send_turn_refuses_when_tmux_session_is_absent():
    # Arrange
    runtime = HermesTuiSessionRuntime(multiplexer=_Mux(exists=False))
    # Act
    delivered = runtime.send_turn(_config(), "not lost", wait_ready=False)
    # Assert
    assert delivered is False


def test_recovery_uses_supported_same_session_controls_in_order():
    # Arrange
    config = _config()
    config.model = "qwen38-27b"
    config.engine_key = "qwen38-27b"
    mux = _Mux()
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    paused = runtime.suspend_autonomous_turns(config)
    recovered = runtime.recover_turn_admission(config)
    # Assert
    assert (
        paused,
        recovered,
        [event[2] for event in mux.events if event[0] == "settled-submit"],
    ) == (
        True,
        True,
        [
            "/heartbeat pause",
            "/model qwen38-27b --provider sac-qwen38-27b --session",
            "/heartbeat resume",
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
        ["tmux-start", "inbox-start", "recovery-start"],
    )


def test_stop_detaches_inbox_before_tmux_session():
    # Arrange
    runtime = _LifecycleRuntime()
    # Act
    stopped = runtime.stop(_config())
    # Assert
    assert (stopped, runtime.lifecycle_events) == (
        True,
        ["inbox-stop", "recovery-stop", "tmux-stop"],
    )
