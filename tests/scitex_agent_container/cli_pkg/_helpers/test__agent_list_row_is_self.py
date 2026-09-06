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
from contextlib import contextmanager

import pytest

from scitex_agent_container.cli_pkg._helpers._agent_list_row import build_agent_row

# The CANONICAL board identity, read first, and its RETIRED predecessor, read
# only as a fallback for a container still launched from an old-name spec.
_VAR = "SCITEX_CARDS_AGENT_ID"
_RETIRED_VAR = "SCITEX_TODO_AGENT_ID"
_ME = "scitex-agent-container"
_SOMEONE_ELSE = "handyman-01"

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


@contextmanager
def _identity_env(**values: str | None) -> Iterator[None]:
    """Pin BOTH identity vars for real, restore whatever was there.

    Every var not named in ``values`` is REMOVED, so a test never inherits
    the runner's own live identity through the variable it is not pinning.
    """
    previous = {var: os.environ.get(var) for var in (_VAR, _RETIRED_VAR)}
    for var in previous:
        value = values.get(var)
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    try:
        yield
    finally:
        for var, value in previous.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


@pytest.fixture
def identified_as_me() -> Iterator[None]:
    """Canonical variable only — how a current spec identifies its agent."""
    with _identity_env(**{_VAR: _ME}):
        yield


@pytest.fixture
def identified_by_the_retired_var() -> Iterator[None]:
    """Retired variable only — a container still on an old-name spec."""
    with _identity_env(**{_RETIRED_VAR: _ME}):
        yield


@pytest.fixture
def identified_by_both() -> Iterator[None]:
    """Both set, DISAGREEING — a spec caught mid-migration."""
    with _identity_env(**{_VAR: _ME, _RETIRED_VAR: _SOMEONE_ELSE}):
        yield


@pytest.fixture
def identified_as_nobody() -> Iterator[None]:
    """Remove the identity entirely — a human at a shell, not an agent."""
    with _identity_env():
        yield


def test_marks_the_caller(identified_as_me: None) -> None:
    # Arrange — the caller identifies itself the same way it stamps card writes.
    name = _ME
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert row["is_self"] is True


def test_marks_the_caller_from_the_retired_var(
    identified_by_the_retired_var: None,
) -> None:
    # Arrange — a container still launched from an old-name spec carries only
    # the retired variable; the control must not vanish for it.
    name = _ME
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert row["is_self"] is True


def test_canonical_var_wins_over_the_retired_one(identified_by_both: None) -> None:
    # Arrange — both set and disagreeing: the CANONICAL name is the identity,
    # so the row it names is the caller's.
    name = _ME
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert row["is_self"] is True


def test_retired_var_does_not_mark_a_peer_row(identified_by_both: None) -> None:
    # Arrange — the stale value must not mark a SECOND row as the caller; two
    # self rows would destroy the control the marker exists to provide.
    name = _SOMEONE_ELSE
    # Act
    row = build_agent_row(name=name, **_ROW_KWARGS)
    # Assert
    assert "is_self" not in row


def test_omits_for_peers(identified_as_me: None) -> None:
    # Arrange — a peer's row must NOT claim to be the caller, or the control is
    # worse than useless: it would validate the listing against the wrong agent.
    name = _SOMEONE_ELSE
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
