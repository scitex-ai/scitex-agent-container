"""Hermes' official TUI adapter uses the shared tmux runtime boundary."""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._claude_spec import ClaudeSpec
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime


class _Mux:
    def __init__(self, exists: bool = True):
        self.alive = exists
        self.events: list[tuple[str, str, str | None]] = []

    def exists(self, name: str) -> bool:
        self.events.append(("exists", name, None))
        return self.alive

    def send_text_literal(self, name: str, text: str) -> None:
        self.events.append(("text", name, text))

    def send_keys(self, name: str, key: str) -> None:
        self.events.append(("key", name, key))


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


def test_send_turn_uses_hermes_native_busy_input_queue():
    # Arrange
    mux = _Mux()
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_turn(_config(), "new guidance", wait_ready=False)
    # Assert
    assert (delivered, mux.events) == (
        True,
        [
            ("exists", "tui-scholar", None),
            ("text", "tui-scholar", "new guidance"),
            ("key", "tui-scholar", "Enter"),
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
    assert (paused, recovered, [event[2] for event in mux.events if event[0] == "text"]) == (
        True,
        True,
        [
            "/heartbeat pause",
            "/model qwen38-27b --provider sac-qwen38-27b --session",
            "/heartbeat resume",
        ],
    )


def test_start_attaches_authenticated_inbox_bridge_before_recovery(monkeypatch):
    # Arrange
    from scitex_agent_container.runtimes import (
        _hermes_inbox_bridge_lifecycle as inbox,
    )
    from scitex_agent_container.runtimes import _hermes_stale_recovery as recovery
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    events = []
    monkeypatch.setattr(TuiSessionRuntime, "start", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(inbox, "start_inbox_bridge", lambda _config: events.append("inbox"))
    monkeypatch.setattr(
        recovery, "start_recovery_monitor", lambda _config: events.append("recovery")
    )

    # Act
    started = HermesTuiSessionRuntime(multiplexer=_Mux()).start(_config())

    # Assert
    assert started is True
    assert events == ["inbox", "recovery"]


def test_stop_detaches_inbox_before_tmux_session(monkeypatch):
    # Arrange
    from scitex_agent_container.runtimes import (
        _hermes_inbox_bridge_lifecycle as inbox,
    )
    from scitex_agent_container.runtimes import _hermes_stale_recovery as recovery
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    events = []
    monkeypatch.setattr(inbox, "stop_inbox_bridge", lambda _config: events.append("inbox"))
    monkeypatch.setattr(
        recovery, "stop_recovery_monitor", lambda _config: events.append("recovery")
    )
    monkeypatch.setattr(
        TuiSessionRuntime,
        "stop",
        lambda *_args, **_kwargs: events.append("tmux") or True,
    )

    # Act
    stopped = HermesTuiSessionRuntime(multiplexer=_Mux()).stop(_config())

    # Assert
    assert stopped is True
    assert events == ["inbox", "recovery", "tmux"]
