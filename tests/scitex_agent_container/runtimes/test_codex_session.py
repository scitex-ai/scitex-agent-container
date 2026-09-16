from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_inner_argv import build_inner_argv
from scitex_agent_container.runtimes.codex_session import CodexSessionRuntime


class _ContainerRuntime:
    def __init__(self) -> None:
        self.started = False

    def start(self, config, **kwargs):
        self.started = True
        return True


def test_codex_runtime_dispatches_headless_agent_to_container():
    # Arrange
    container = _ContainerRuntime()
    runtime = CodexSessionRuntime(container_runtime_for=lambda config: container)
    config = AgentConfig(name="codex-worker", harness="codex", runtime="headless")
    runtime._setup_workspace = lambda config: None

    # Act
    started = runtime.start(config)

    # Assert
    assert (started, container.started) == (True, True)


def test_codex_headless_inner_argv_selects_codex_session_daemon():
    # Arrange
    config = AgentConfig(name="codex-worker", harness="codex", runtime="headless")

    # Act
    argv = build_inner_argv(config)

    # Assert
    assert "scitex_agent_container._runners.codex_session" in argv[-1]
