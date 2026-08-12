"""Tests for ``runtimes/_apptainer_inner_argv_tui.py``.

Covers the interactive-TUI ``--resume <id>`` branch: ``spec.claude.session:
resume`` + ``spec.claude.resume_id`` translates to ``claude --resume <uuid>``
(mirrors the legacy tmux runner), distinct from the bare ``-c``
(latest-for-home) continue path. No mocks: the real builder is exercised
on a real config.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes._apptainer_inner_argv_tui import (
    _home_has_resumable_conversation,
    _tui_runner_argv,
)

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


# ---------------------------------------------------------------------------
# Continue-gate: the gate must look where the SPEC writes the transcript.
# Measured 2026-08-11 on the canary relocation to scitex-compute-04: the
# transcript arrived byte- and line-verified, the marker was seeded, the agent
# started - and started FRESH. The gate checked the overlay upper-home (which
# existed and was empty) and never the bind carrying the transcript, so -c was
# silently omitted and 3.7 MB of conversation one directory away was not
# resumed. Nothing reported a problem.
# ---------------------------------------------------------------------------
def _config(name: str, *, binds: list[str], overlay_raw: str = ""):
    """An AgentConfig-shaped object carrying only what the gate reads."""
    raw_args = ["--overlay", overlay_raw] if overlay_raw else []
    return SimpleNamespace(
        name=name,
        runtime="tui",
        config_path="",
        apptainer=SimpleNamespace(binds=binds, overlay="", raw_args=raw_args),
    )


@pytest.fixture()
def bound_agent(tmp_path):
    """A spec that BINDS its transcript dir, with a real transcript in it.

    The shape the fleet's canary carries and the shape a relocation produces:
    ``<runtime>/home/.claude/projects`` bound onto the container's
    ``/home/agent/.claude/projects``, plus an overlay that EXISTS and holds
    nothing — exactly the pair that produced the silent fresh start.
    """
    runtime_home = tmp_path / "runtime" / "home"
    projects = runtime_home / ".claude" / "projects" / "-proj-canary"
    projects.mkdir(parents=True)
    (projects / "afb6da24.jsonl").write_text('{"type":"user"}\n')

    overlay = tmp_path / "overlays" / "canary"
    (overlay / "upper" / "home" / "agent" / ".claude" / "projects").mkdir(parents=True)

    config = _config(
        f"gate-bound-{tmp_path.name}",
        binds=[f"{runtime_home}/.claude/projects:/home/agent/.claude/projects:rw"],
        overlay_raw=str(overlay),
    )
    return config, runtime_home


def test_a_transcript_reached_only_by_a_bind_is_found(bound_agent) -> None:
    # Arrange: THE regression. The overlay upper exists and is empty; the
    # transcript is reachable only through the bind.
    config, _ = bound_agent
    # Act
    has, _home = _home_has_resumable_conversation(config)
    # Assert
    assert has is True


def test_the_home_reported_is_the_one_holding_the_transcript(bound_agent) -> None:
    # Arrange: the warning logged on a miss names this directory, so naming a
    # directory that never held transcripts sends the reader to the wrong place.
    config, runtime_home = bound_agent
    # Act
    _has, home = _home_has_resumable_conversation(config)
    # Assert
    assert home == runtime_home


def test_an_agent_with_no_transcript_anywhere_reports_none_found(tmp_path) -> None:
    # Arrange: the gate's original reason for existing — `claude -c` with no
    # conversation prints "No conversation found to continue" and EXITS, killing
    # the tmux PTY at boot. Searching more places must not weaken that.
    runtime_home = tmp_path / "runtime" / "home"
    (runtime_home / ".claude" / "projects").mkdir(parents=True)
    config = _config(
        f"gate-empty-{tmp_path.name}",
        binds=[f"{runtime_home}/.claude/projects:/home/agent/.claude/projects:rw"],
    )
    # Act
    has, _home = _home_has_resumable_conversation(config)
    # Assert
    assert has is False


def test_an_overlay_only_spec_still_finds_its_transcript(tmp_path) -> None:
    # Arrange: the case that already worked and must keep working — no bind
    # covers the container home, so the writes land in the overlay's upper layer.
    overlay = tmp_path / "overlays" / "ov"
    projects = overlay / "upper" / "home" / "agent" / ".claude" / "projects" / "-p"
    projects.mkdir(parents=True)
    (projects / "sess.jsonl").write_text('{"type":"user"}\n')
    config = _config(f"gate-ov-{tmp_path.name}", binds=[], overlay_raw=str(overlay))
    # Act
    has, _home = _home_has_resumable_conversation(config)
    # Assert
    assert has is True
