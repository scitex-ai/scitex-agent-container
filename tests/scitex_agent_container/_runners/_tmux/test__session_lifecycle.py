"""Day-2 (D) tests for ``_session_lifecycle``.

The lifecycle module owns:
* ``build_command`` — assembling the ``claude`` CLI argv.
* ``build_env_exports`` — assembling the export prelude.
* Session-resume probe + helpers.

These tests pin the observable shapes without spinning up real tmux
or real claude.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scitex_agent_container._runners._tmux._session_lifecycle import (
    _encode_workdir_for_claude_projects,
    build_command,
    build_env_exports,
    build_env_source_prelude,
    needs_auto_accept,
)


@dataclass
class _FakeRemote:
    is_remote: bool = False
    host: str = ""


@dataclass
class _FakeClaude:
    flags: list[str] = field(default_factory=list)
    session: str = "new-session"
    continue_max_age_minutes: int | None = None
    resume_id: str = ""
    auto_accept: bool = True
    runtime: str = "tmux"


@dataclass
class _FakeConfig:
    """Minimum surface AgentConfig-like for the lifecycle helpers."""

    name: str = "demo"
    model: str = "opus"
    expanded_workdir: str = "/tmp/demo-workdir"
    env: dict = field(default_factory=dict)
    env_files: list = field(default_factory=list)
    claude: _FakeClaude = field(default_factory=_FakeClaude)
    remote: _FakeRemote = field(default_factory=_FakeRemote)


def test_build_command_starts_with_claude_binary():
    # Arrange
    config = _FakeConfig()
    # Act
    cmd = build_command(config)
    # Assert
    assert cmd.startswith("claude ")


def test_build_command_includes_model_flag():
    # Arrange
    config = _FakeConfig(model="opus[1m]")
    # Act
    cmd = build_command(config)
    # Assert
    assert "--model 'opus[1m]'" in cmd


def test_build_command_includes_add_dir_for_workdir():
    # Arrange
    config = _FakeConfig(expanded_workdir="/tmp/my-work")
    # Act
    cmd = build_command(config)
    # Assert
    assert "--add-dir '/tmp/my-work'" in cmd


def test_build_command_for_new_session_omits_continue():
    """``session=new-session`` must NOT add ``--continue``."""
    # Arrange
    config = _FakeConfig()
    config.claude.session = "new-session"
    # Act
    cmd = build_command(config)
    # Assert
    assert "--continue" not in cmd


def test_build_command_appends_user_flags_verbatim():
    # Arrange
    config = _FakeConfig()
    config.claude.flags = ["--dangerously-skip-permissions"]
    # Act
    cmd = build_command(config)
    # Assert
    assert "--dangerously-skip-permissions" in cmd


def test_build_env_exports_sources_env_files_first():
    # Arrange
    config = _FakeConfig(
        env={"A": "1"},
        env_files=[".env.local"],
    )
    # Act
    out = build_env_exports(config)
    # Assert
    lines = out.splitlines()
    # .env file source line precedes the export A=1.
    assert any("./.env.local" in line for line in lines)
    assert any('export A="1"' in line for line in lines)


def test_build_env_source_prelude_emits_set_a_pattern():
    # Arrange
    workdir = "/tmp/x"
    # Act
    out = build_env_source_prelude(workdir)
    # Assert
    assert "/tmp/x/.env" in out
    assert "set -a" in out
    assert "set +a" in out


def test_encode_workdir_collapses_triple_dashes():
    """Triple-dash runs from "/." in path must collapse to "--"."""
    # Arrange
    workdir = "/home/agent/.scitex/x"
    # Act
    encoded = _encode_workdir_for_claude_projects(workdir)
    # Assert
    assert "---" not in encoded


def test_needs_auto_accept_false_when_disabled():
    # Arrange
    config = _FakeConfig()
    config.claude.auto_accept = False
    config.claude.flags = ["--dangerously-skip-permissions"]
    # Act
    out = needs_auto_accept(config)
    # Assert
    assert out is False


def test_needs_auto_accept_false_without_dangerous_flag():
    # Arrange
    config = _FakeConfig()
    config.claude.flags = []
    # Act
    out = needs_auto_accept(config)
    # Assert
    assert out is False


def test_needs_auto_accept_true_with_dangerous_flag():
    # Arrange
    config = _FakeConfig()
    config.claude.auto_accept = True
    config.claude.flags = ["--dangerously-skip-permissions"]
    # Act
    out = needs_auto_accept(config)
    # Assert
    assert out is True


# ---------------------------------------------------------------------------
# ClaudeCodeRuntime lifecycle wiring — uses an in-memory fake multiplexer
# so the tests run without tmux installed.
# ---------------------------------------------------------------------------


class _FakeMux:
    """Records every multiplexer call so the orchestrator wiring can be asserted."""

    def __init__(self):
        self.start_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self.exists_calls: list[str] = []
        self.capture_calls: list[str] = []
        self.capture_logs_calls: list[tuple] = []
        self.send_calls: list[tuple] = []
        self._exists = False

    def exists(self, session_name: str) -> bool:
        self.exists_calls.append(session_name)
        return self._exists

    def start(
        self,
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
    ) -> bool:
        self.start_calls.append(
            {
                "session_name": session_name,
                "command": command,
                "workdir": workdir,
                "env_exports": env_exports,
                "venv": venv,
            }
        )
        self._exists = True
        return True

    def stop(self, session_name: str) -> bool:
        self.stop_calls.append(session_name)
        self._exists = False
        return True

    def capture_content(self, session_name: str) -> str:
        self.capture_calls.append(session_name)
        return ""

    def capture_logs(self, session_name: str, lines: int = 50) -> str:
        self.capture_logs_calls.append((session_name, lines))
        return ""

    def send_keys(self, session_name: str, *keys: str) -> None:
        self.send_calls.append((session_name, *keys))


@dataclass
class _FakeContainer:
    runtime: str = "none"


@dataclass
class _FakeRuntimeConfig:
    name: str = "demo"
    model: str = "opus"
    expanded_workdir: str = "/tmp/demo"
    env: dict = field(default_factory=dict)
    env_files: list = field(default_factory=list)
    python_venv: str = ""
    screen_name: str = "sac-demo"
    startup_commands: list = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    config_path: str = ""
    claude: _FakeClaude = field(default_factory=_FakeClaude)
    remote: _FakeRemote = field(default_factory=_FakeRemote)
    container: _FakeContainer = field(default_factory=_FakeContainer)
    startup: Any = None


def test_runtime_start_calls_multiplexer_with_built_command(tmp_path, monkeypatch):
    """Session bring-up issues the expected tmux start invocation.

    Asserts the orchestrator wires ``build_command`` output through to
    the multiplexer's ``start`` call.
    """
    from scitex_agent_container._runners._tmux import claude_code as cc

    fake_mux = _FakeMux()
    config = _FakeRuntimeConfig(expanded_workdir=str(tmp_path))

    # Arrange — inject the fake mux and silence the workspace setup +
    # a2a sidecar so the test only exercises the lifecycle hook. The
    # ``setup_workspace`` symbol is re-imported INTO ``claude_code``
    # via ``from ._session_lifecycle import setup_workspace``, so the
    # patch must target the bound name on ``cc`` (not the source).
    monkeypatch.setattr(cc.ClaudeCodeRuntime, "_get_mux", lambda self, c: fake_mux)
    monkeypatch.setattr(cc, "setup_workspace", lambda c, w: None)
    monkeypatch.setattr(cc, "_a2a_start_sidecar", lambda c: None)

    # Act
    started = cc.ClaudeCodeRuntime().start(config)

    # Assert
    assert started is True
    assert len(fake_mux.start_calls) == 1
    call = fake_mux.start_calls[0]
    assert call["session_name"] == "sac-demo"
    assert call["command"].startswith("claude ")
    assert call["workdir"] == str(tmp_path)


def test_runtime_stop_calls_multiplexer_stop_with_session_name(monkeypatch):
    """Teardown delegates to the multiplexer's ``stop`` (kill-session equivalent)."""
    from scitex_agent_container._runners._tmux import claude_code as cc

    fake_mux = _FakeMux()
    fake_mux._exists = True
    config = _FakeRuntimeConfig()

    monkeypatch.setattr(cc.ClaudeCodeRuntime, "_get_mux", lambda self, c: fake_mux)
    monkeypatch.setattr(cc, "cleanup_workspace", lambda c, w: None)
    monkeypatch.setattr(cc, "_a2a_stop_sidecar", lambda c: None)

    # Act
    cc.ClaudeCodeRuntime().stop(config)

    # Assert
    assert fake_mux.stop_calls == ["sac-demo"]


def test_runtime_is_running_delegates_to_multiplexer_exists(monkeypatch):
    from scitex_agent_container._runners._tmux import claude_code as cc

    fake_mux = _FakeMux()
    fake_mux._exists = True
    config = _FakeRuntimeConfig()

    monkeypatch.setattr(cc.ClaudeCodeRuntime, "_get_mux", lambda self, c: fake_mux)

    # Act
    out = cc.ClaudeCodeRuntime().is_running(config)

    # Assert
    assert out is True
    assert fake_mux.exists_calls == ["sac-demo"]
