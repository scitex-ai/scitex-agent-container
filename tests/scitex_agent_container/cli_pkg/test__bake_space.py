"""Tests for the bake-remote free-space decision.

The incident: a ~7.6G transfer started onto a volume with 4.0G free,
could not finish, and left a partial that made the next attempt likelier
to fail.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.cli_pkg._bake_space import (
    DEFAULT_MARGIN_BYTES,
    SpaceVerdict,
    check_space,
    human_bytes,
)

GIB = 1024**3


def test_a_pull_that_fits_proceeds() -> None:
    # Arrange
    free = 20 * GIB
    # Act
    verdict = check_space(remote_size=7 * GIB, existing_partial=0, free=free)
    # Assert
    assert verdict.proceed is True


def test_a_pull_that_cannot_fit_is_refused() -> None:
    """The measured incident: 7.6G wanted, 4.0G free."""
    # Arrange
    free = 4 * GIB
    # Act
    verdict = check_space(remote_size=8 * GIB, existing_partial=0, free=free)
    # Assert
    assert verdict.proceed is False


def test_the_refusal_names_the_free_space() -> None:
    """The operator's next action must not require a guess."""
    # Arrange
    free = 4 * GIB
    # Act
    verdict = check_space(remote_size=8 * GIB, existing_partial=0, free=free)
    # Assert
    assert human_bytes(free) in verdict.reason


def test_a_resume_only_needs_the_REMAINDER() -> None:
    """--partial resumes, so a nearly-complete pull must not be refused.

    7.6G artifact with 7.0G already on disk needs 0.6G, not 7.6G. A
    checker demanding the whole artifact would refuse work that fits.
    """
    # Arrange
    free = 3 * GIB
    # Act
    verdict = check_space(
        remote_size=8 * GIB, existing_partial=int(7.5 * GIB), free=free
    )
    # Assert
    assert verdict.proceed is True


def test_the_margin_is_demanded_on_top_of_the_transfer() -> None:
    """Landing with zero to spare leaves a host that cannot write logs."""
    # Arrange
    remote = 4 * GIB
    free = remote + DEFAULT_MARGIN_BYTES - 1
    # Act
    verdict = check_space(remote_size=remote, existing_partial=0, free=free)
    # Assert
    assert verdict.proceed is False


def test_an_unknown_size_proceeds_rather_than_blocking() -> None:
    """A flaky probe must not stop every pull."""
    # Arrange
    free = 4 * GIB
    # Act
    verdict = check_space(remote_size=None, existing_partial=0, free=free)
    # Assert
    assert verdict.proceed is True


def test_an_unknown_size_is_marked_unknown_not_fitting() -> None:
    """'cannot tell' must stay distinguishable from 'it fits'."""
    # Arrange
    free = 4 * GIB
    # Act
    verdict = check_space(remote_size=None, existing_partial=0, free=free)
    # Assert
    assert verdict.known is False


def test_a_measured_verdict_is_marked_known() -> None:
    # Arrange
    free = 20 * GIB
    # Act
    verdict = check_space(remote_size=7 * GIB, existing_partial=0, free=free)
    # Assert
    assert verdict.known is True


def test_an_unknown_verdict_that_refuses_is_rejected_at_construction() -> None:
    """The validator forbids the shape that would block every pull."""
    # Arrange
    kwargs = dict(proceed=False, known=False, needed=0, free=0, reason="x")
    # Act
    # Assert
    with pytest.raises(ValueError):
        SpaceVerdict(**kwargs)


def test_a_partial_larger_than_the_artifact_does_not_go_negative() -> None:
    """Defensive: a stale oversized partial must not produce a negative need."""
    # Arrange
    free = 3 * GIB
    # Act
    verdict = check_space(
        remote_size=1 * GIB, existing_partial=9 * GIB, free=free
    )
    # Assert
    assert verdict.needed == DEFAULT_MARGIN_BYTES
