"""The continue-gate must look where the spec actually writes the transcript.

Measured 2026-08-11 on the canary-resume-test relocation to scitex-compute-04:
the transcript arrived byte- and line-verified, the session marker was seeded,
the agent started — and started FRESH. The gate checked the overlay upper-home,
which existed and was empty, and never looked at the bind that carries the
transcript, so ``-c`` was silently omitted and 3.7 MB of conversation sitting one
directory away was not resumed. Nothing reported a problem.

Real directories and real bytes throughout: each case builds the host tree the
spec describes under ``tmp_path`` and asks the production function about it. The
workspace-home fallback is the LAST candidate and resolves to a per-test unique
agent name, so it never exists and never decides an answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._apptainer_inner_argv_tui import (
    _home_has_resumable_conversation,
)


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
