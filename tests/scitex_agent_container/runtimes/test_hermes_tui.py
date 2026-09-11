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
