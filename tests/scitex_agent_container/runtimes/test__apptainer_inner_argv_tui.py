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


# ─── session fork (--fork-session / --session-id) ─────────────────────────

_FORK_PARENT_UUID = "123e4567-e89b-12d3-a456-426614174000"
_FORK_NEW_UUID = "fe612a87-4091-5db2-a9c8-ddb2ab6ad430"


def _fork_cfg(**claude_kw) -> AgentConfig:
    """A resume-mode config carrying the fork pair (what a twin's first boot
    looks like after ``seed_twin_from_parent`` overrides the in-memory spec)."""
    return AgentConfig(
        name="p-forked-t",
        runtime="tui",
        claude=ClaudeSpec(model="haiku", **claude_kw),
    )


def test_tui_argv_forks_the_resumed_parent_session():
    # Arrange — the shape ADR-0019's amendment prescribes.
    cfg = _fork_cfg(
        session="resume",
        resume_id=_FORK_PARENT_UUID,
        fork_session=True,
        session_id=_FORK_NEW_UUID,
    )
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--fork-session" in argv


def test_tui_argv_pins_the_forked_session_id():
    # Arrange
    cfg = _fork_cfg(
        session="resume",
        resume_id=_FORK_PARENT_UUID,
        fork_session=True,
        session_id=_FORK_NEW_UUID,
    )
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert argv[argv.index("--session-id") + 1] == _FORK_NEW_UUID


def test_tui_argv_forks_from_the_parents_uuid():
    # Arrange — resume names the PARENT's session; the fork writes elsewhere.
    cfg = _fork_cfg(
        session="resume",
        resume_id=_FORK_PARENT_UUID,
        fork_session=True,
        session_id=_FORK_NEW_UUID,
    )
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert argv[argv.index("--resume") + 1] == _FORK_PARENT_UUID


def test_tui_argv_omits_fork_flags_on_a_fresh_session():
    # Arrange — claude documents both flags as resume-only, and SILENTLY
    # ignores --fork-session on a fresh session; emitting them anyway would
    # make a twin look booted while having inherited nothing.
    cfg = _fork_cfg(session="fresh", fork_session=True, session_id=_FORK_NEW_UUID)
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--fork-session" not in argv and "--session-id" not in argv


def test_tui_argv_has_no_fork_flags_by_default():
    # Arrange — a plain resume must be untouched by this feature.
    cfg = _fork_cfg(session="resume", resume_id=_FORK_PARENT_UUID)
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--fork-session" not in argv and "--session-id" not in argv
