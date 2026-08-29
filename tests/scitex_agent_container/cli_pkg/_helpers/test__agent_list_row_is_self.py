#!/usr/bin/env python3
"""``is_self`` on the caller's own agent-list row.

WHY THIS GATE EXISTS. ``sac agents list`` is vantage-point dependent: read from
inside a container it reports SPEC DEFINITIONS, and on 2026-08-16 it returned
``running=0`` for a fleet where eight handymen and three maintainers were
provably working. scitex-hpc hit the same thing from a different container and
found the decisive detail — their OWN row read ``status: defined`` while they
were executing the command that produced it.

So the self row is a free control: if the listing is wrong about the one agent
the caller can verify with certainty, its answer about every other row carries
no information. These tests keep that marker present, absent where it would be a
lie, and silent when there is no identity to compare against.

The environment is mutated FOR REAL and restored on teardown rather than patched
— the production code reads ``os.environ`` and so does the test, so a rename of
the variable breaks this gate instead of sliding past a stubbed lookup.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from scitex_agent_container.cli_pkg._helpers._agent_list_row import build_agent_row

_VAR = "SCITEX_TODO_AGENT_ID"
_ME = "scitex-agent-container"

_ROW_KWARGS = dict(
    status_val="running",
    screen_name="tui-x",
    multiplexer="tmux",
    started="2026-08-16T00:00:00Z",
    host_label="scitex-compute-04",
    host_display="scitex-compute-04",
    spec_path="/spec.yaml",
    a2a_port=19003,
    account_label="acct",
    deferred=True,
    errors=None,
    liveness_unknown=False,
    labels=None,
)


@pytest.fixture
def identified_as_me() -> Iterator[None]:
    """Set the real agent-identity variable, restore whatever was there."""
    previous = os.environ.get(_VAR)
    os.environ[_VAR] = _ME
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_VAR, None)
        else:
            os.environ[_VAR] = previous


@pytest.fixture
def identified_as_nobody() -> Iterator[None]:
    """Remove the identity entirely — a human at a shell, not an agent."""
    previous = os.environ.get(_VAR)
    os.environ.pop(_VAR, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[_VAR] = previous


def test_marks_the_caller(identified_as_me: None) -> None:
    # Arrange — the caller identifies itself the same way it stamps card writes.
    name = _ME
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert row["is_self"] is True


def test_omits_for_peers(identified_as_me: None) -> None:
    # Arrange — a peer's row must NOT claim to be the caller, or the control is
    # worse than useless: it would validate the listing against the wrong agent.
    name = "handyman-01"
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert "is_self" not in row


def test_absent_without_identity(identified_as_nobody: None) -> None:
    # Arrange — with no agent identity, marking any row would invent a control
    # that does not exist.
    name = _ME
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert "is_self" not in row
