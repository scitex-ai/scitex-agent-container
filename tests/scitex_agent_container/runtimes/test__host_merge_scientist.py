"""Scientist-group host ``~/.claude`` deep-merge gate (operator 2026-06-25).

Sub-goal 3 of the scientist-group ACL work: a scientist agent
(``metadata.labels.group: scientist`` + ``role: project-maintainer`` —
e.g. paper-scitex-clew / paper-neurovista / paper-ripple-wm) must get
the SAME FULL-developer host deep-merge as a developer-group agent.

The gate :func:`scitex_agent_container.runtimes._host_merge.is_full_developer`
now accepts ``scientist`` in its ``_FULL_MERGE_GROUPS`` allowlist; an
unrelated explicit group (e.g. ``solitary``) is still excluded.

scitex doctrine: NO mocks/monkeypatch — pure AgentConfig in, bool out.
AAA on own lines, one assert per test.
"""

from __future__ import annotations

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._host_merge import is_full_developer


def test_scientist_group_with_maintainer_role_is_full_developer() -> None:
    """group=scientist + role=project-maintainer → FULL deep-merge."""
    # Arrange
    cfg = AgentConfig(name="paper-scitex-clew")
    cfg.labels = {"group": "scientist", "role": "project-maintainer"}
    # Act
    result = is_full_developer(cfg)
    # Assert
    assert result is True


def test_scientist_group_alone_is_full_developer() -> None:
    """group=scientist (no role) → FULL deep-merge (group wins outright)."""
    # Arrange
    cfg = AgentConfig(name="paper-neurovista")
    cfg.labels = {"group": "scientist"}
    # Act
    result = is_full_developer(cfg)
    # Assert
    assert result is True


def test_unrelated_explicit_group_still_excluded() -> None:
    """A non-full-merge explicit group (solitary) stays excluded."""
    # Arrange
    cfg = AgentConfig(name="capsule")
    cfg.labels = {"group": "solitary", "role": "project-maintainer"}
    # Act
    result = is_full_developer(cfg)
    # Assert
    assert result is False
