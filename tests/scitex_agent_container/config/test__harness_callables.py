"""Resolved lifecycle session modes at the Hermes TUI boundary."""

from __future__ import annotations

import pytest

from scitex_agent_container.cli_pkg.lifecycle._start_single import (
    should_preflight_claude_resume,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._claude_spec import ClaudeSpec
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes import _hermes_tui_owner as owner
from scitex_agent_container.runtimes.hermes_tui import (
    HermesTuiSessionRuntime,
    _hermes_pane_boot_ready,
)


def _config(*, session: str, resume_id: str = "") -> AgentConfig:
    config = AgentConfig(
        name="cards",
        harness="hermes",
        runtime="tui",
        claude=ClaudeSpec(session=session, resume_id=resume_id),
    )
    config.engine_key = "qwen38-27b"
    config.model = "qwen38-27b"
    return config


def _session_tail(argv: list[str]) -> list[str]:
    for flag in ("--continue", "--resume"):
        if flag in argv:
            return argv[argv.index(flag) :]
    return []


@pytest.mark.parametrize(
    ("session", "resume_id", "expected_tail"),
    [
        ("fresh", "", []),
        (
            "continue",
            "",
            ["--continue", "sac:cards:qwen38-27b", "--create-if-missing"],
        ),
        ("resume", "session-20260910", ["--resume", "session-20260910"]),
    ],
)
def test_resolved_backend_reaches_every_hermes_session_mode(
    session, resume_id, expected_tail
):
    # Arrange
    config = _config(session=session, resume_id=resume_id)
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert — no explicit model/provider override: the generated hermes
    # config.yaml carries the default, and the override path trips the
    # data-training-tier guard in non-interactive runs.
    assert "--model" not in argv
    assert "--provider" not in argv
    assert _session_tail(argv) == expected_tail


def test_hermes_refuses_an_unresolved_backend_instead_of_showing_setup():
    # Arrange
    config = AgentConfig(name="cards", harness="hermes", runtime="tui")
    config.model = ""
    # Act
    call = lambda: _hermes_tui_inner_argv(config)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="resolved engine model and key"):
        call()


def test_hermes_refuses_model_as_an_implicit_engine_key():
    # Arrange
    config = _config(session="continue")
    config.engine_key = ""

    # Act
    call = lambda: _hermes_tui_inner_argv(config)  # noqa: E731

    # Assert
    with pytest.raises(ValueError, match="resolved engine model and key"):
        call()


def test_hermes_engine_change_selects_a_different_continuation_name():
    # Arrange
    qwen = _config(session="continue")
    gpt = _config(session="continue")
    gpt.engine_key = "gpt-sol"
    gpt.model = "gpt-5.6-sol"

    # Act
    qwen_tail = _session_tail(_hermes_tui_inner_argv(qwen))
    gpt_tail = _session_tail(_hermes_tui_inner_argv(gpt))

    # Assert
    assert (qwen_tail, gpt_tail) == (
        ["--continue", "sac:cards:qwen38-27b", "--create-if-missing"],
        ["--continue", "sac:cards:gpt-sol", "--create-if-missing"],
    )


def test_explicit_resume_reaches_native_hermes_argv():
    # Arrange
    config = _config(session="resume", resume_id="session-20260910")
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert argv[-2:] == ["--resume", "session-20260910"]


def test_resume_without_an_id_refuses_instead_of_starting_fresh():
    # Arrange
    config = _config(session="resume")
    # Act
    call = lambda: _hermes_tui_inner_argv(config)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="requires.*resume_id"):
        call()


def test_hermes_resume_id_bypasses_claude_transcript_preflight():
    # Arrange
    config = _config(session="resume", resume_id="20260910_072303_526792")
    # Act
    should_preflight = should_preflight_claude_resume(config, config.claude.resume_id)
    # Assert
    assert should_preflight is False


def test_setup_required_is_not_boot_readiness():
    # Arrange
    pane = "─ setup required │ qwen ─ /work\n ❯\n ☤ setup required"
    # Act
    ready = _hermes_pane_boot_ready(pane)
    # Assert
    assert ready is False


def test_bound_ready_composer_is_boot_readiness():
    # Arrange
    pane = "─ ready │ qwen38 27b low │ 99% ─ /work\n ❯ "
    # Act
    ready = _hermes_pane_boot_ready(pane)
    # Assert
    assert ready is True


class _SetupMux:
    def exists(self, _name: str) -> bool:
        return True

    def capture_content(self, _name: str) -> str:
        return "─ setup required │ no model ─ /work\n ❯ "


def test_hermes_boot_drain_fails_fast_on_setup_required():
    # Arrange
    runtime = HermesTuiSessionRuntime(multiplexer=_SetupMux())
    # Act
    ready = runtime._drain_at_boot(_config(session="fresh"), timeout_s=30)
    # Assert
    assert ready is False


class _Process:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_fresh_session_transport_recovery_resumes_the_observed_id(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        process = _Process()
        spawned.append((list(command), env, process))
        return process

    snapshots = iter(
        ([{"id": "fresh-42", "title": "generated"}], [], [], [{"id": "fresh-42"}])
    )

    def active_list(_state_dir):
        try:
            return next(snapshots)
        except StopIteration:
            spawned[-1][2].returncode = 0
            return [{"id": "fresh-42"}]

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--query", "initial task"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=active_list,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )
    # Assert
    assert (result, spawned[1][0][-2:], "--query" in spawned[1][0]) == (
        0,
        ["--resume", "fresh-42"],
        False,
    )
