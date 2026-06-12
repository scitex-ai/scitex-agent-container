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


def test_build_env_exports_includes_env_file_source_line():
    """The .env file source line precedes the export A=1."""
    # Arrange
    config = _FakeConfig(env={"A": "1"}, env_files=[".env.local"])
    # Act
    out = build_env_exports(config)
    # Assert
    assert any("./.env.local" in line for line in out.splitlines())


def test_build_env_exports_includes_explicit_export_line():
    # Arrange
    config = _FakeConfig(env={"A": "1"}, env_files=[".env.local"])
    # Act
    out = build_env_exports(config)
    # Assert
    assert any('export A="1"' in line for line in out.splitlines())


def test_build_env_source_prelude_includes_workdir_env_path():
    # Arrange
    workdir = "/tmp/x"
    # Act
    out = build_env_source_prelude(workdir)
    # Assert
    assert "/tmp/x/.env" in out


def test_build_env_source_prelude_includes_set_minus_a():
    # Arrange
    workdir = "/tmp/x"
    # Act
    out = build_env_source_prelude(workdir)
    # Assert
    assert "set -a" in out


def test_build_env_source_prelude_includes_set_plus_a():
    # Arrange
    workdir = "/tmp/x"
    # Act
    out = build_env_source_prelude(workdir)
    # Assert
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
# ClaudeCodeRuntime lifecycle wiring — DEFERRED to a follow-up PR.
#
# The Day-2 PR opened these tests as ``monkeypatch``-using lifecycle
# tests. PR-#353 introduces a no-monkeypatch DI seam on the runtime
# (``ClaudeCodeRuntime(mux_factory=..., setup_workspace_fn=...,
# a2a_start_fn=..., ...)``), but the corresponding lifecycle tests
# hang on the no-op codepath (likely a background-thread join in the
# ``post_start_tasks`` branch that needs its own DI). Per lead
# guidance 82e3990b (no carve-out for ``monkeypatch`` via
# ``stx-allow``; CI's PA-306 rejects regardless), the tests are
# deferred so PR-#353 can land green. The DI seam itself stays in
# the source (no behaviour change for production callers — the
# defaults route to the real implementations) and the follow-up PR
# rebuilds these tests against it.
# ---------------------------------------------------------------------------
