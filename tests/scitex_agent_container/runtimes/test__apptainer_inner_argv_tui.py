"""Tests for ``runtimes/_apptainer_inner_argv_tui.py``.

Covers the interactive-TUI ``--resume <id>`` branch: ``spec.claude.session:
resume`` + ``spec.claude.resume_id`` translates to ``claude --resume <uuid>``
(mirrors the legacy tmux runner), distinct from the bare ``-c``
(latest-for-home) continue path. No mocks: the real builder is exercised
on a real config.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes._apptainer_inner_argv_tui import _tui_runner_argv

_VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"


def _cfg(*, session: str, resume_id: str = "") -> AgentConfig:
    return AgentConfig(
        name="t-tui",
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session=session, resume_id=resume_id),
    )


def test_resume_with_id_emits_resume_flag():
    # Arrange
    cfg = _cfg(session="resume", resume_id=_VALID_UUID)
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--resume" in argv


def test_resume_flag_is_followed_by_the_pinned_uuid():
    # Arrange
    cfg = _cfg(session="resume", resume_id=_VALID_UUID)
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert argv[argv.index("--resume") + 1] == _VALID_UUID


def test_resume_never_emits_bare_continue_flag():
    # Arrange — id-addressed resume must not degrade to bare ``-c``.
    cfg = _cfg(session="resume", resume_id=_VALID_UUID)
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "-c" not in argv


def test_resume_without_id_omits_resume_flag():
    # Arrange — session=resume but not pinned → fall through to fresh.
    cfg = _cfg(session="resume", resume_id="")
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--resume" not in argv
