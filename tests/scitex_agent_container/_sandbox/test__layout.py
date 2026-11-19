"""Sealed-run layout: the container never sees ``out/`` or ``secrets/``."""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._sandbox._argv import sandbox_flags
from scitex_agent_container._sandbox._layout import RunPaths, run_link


def test_run_link_lives_under_the_state_dir(tmp_path):
    # Arrange
    state = tmp_path / "state"
    # Act
    link = run_link(state)
    # Assert
    assert link == state / "sandbox" / "run"


def test_outcome_is_under_out(tmp_path):
    # Arrange
    rp = RunPaths(tmp_path)
    # Act
    rel = rp.outcome.relative_to(tmp_path)
    # Assert
    assert rel == Path("out/outcome.json")


def test_out_and_secrets_are_never_bind_sources(tmp_path):
    # Arrange
    rp = RunPaths(run_link(tmp_path))
    flags = sandbox_flags(object(), tmp_path)
    # Act
    sources = [
        flags[i + 1].split(":", 1)[0] for i, a in enumerate(flags) if a == "--bind"
    ]
    # Assert
    assert not [s for s in sources if s.startswith((str(rp.out), str(rp.secrets)))]
