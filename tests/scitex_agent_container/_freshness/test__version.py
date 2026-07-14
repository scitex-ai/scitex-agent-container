"""Version ordering. The contract: what it cannot parse, it refuses to judge."""

from __future__ import annotations

from scitex_agent_container._freshness._version import (
    compare,
    is_behind,
    latest,
    parse,
)


def test_orders_by_number_not_string():
    """0.21.9 < 0.21.17. String comparison gets this backwards, and the
    whole 0.21 line lives exactly where it would bite."""
    # Arrange
    # Act
    result = compare("0.21.9", "0.21.17")

    # Assert
    assert result == -1


def test_tag_and_release_compare_equal():
    """v0.21.17 (git) and 0.21.17 (PyPI) are the same release.

    The ghost-tag check is precisely the act of lining these two spellings
    up, so they must compare equal or every tag reads as a ghost.
    """
    # Arrange
    # Act
    result = compare("v0.21.17", "0.21.17")

    # Assert
    assert result == 0


def test_prerelease_sorts_before_final():
    # Arrange
    # Act
    result = compare("0.21.17rc1", "0.21.17")

    # Assert
    assert result == -1


def test_unparseable_version_returns_none():
    """'dev' is not a version. Guessing is how a comparator lies."""
    # Arrange
    # Act
    result = parse("dev")

    # Assert
    assert result is None


def test_compare_with_unparseable_is_unknown():
    # Arrange
    # Act
    result = compare("0.0.0+unknown", "0.21.17")

    # Assert
    assert result is None


def test_older_install_is_behind():
    # Arrange
    # Act
    result = is_behind("0.21.14", "0.21.17")

    # Assert
    assert result is True


def test_equal_install_is_not_behind():
    # Arrange
    # Act
    result = is_behind("0.21.17", "0.21.17")

    # Assert
    assert result is False


def test_newer_install_is_not_behind():
    """Ahead is not behind. A dev build must not be nagged as 'stale'."""
    # Arrange
    # Act
    result = is_behind("0.22.0", "0.21.17")

    # Assert
    assert result is False


def test_behind_is_unknown_when_unparseable():
    # Arrange
    # Act
    result = is_behind("dev", "0.21.17")

    # Assert
    assert result is None


def test_latest_picks_highest_release():
    """Real PyPI list; 0.21.17 is newest despite 0.21.9 sorting later as text."""
    # Arrange
    releases = ["0.21.4", "0.21.9", "0.21.14", "0.21.17", "0.21.11"]

    # Act
    result = latest(releases)

    # Assert
    assert result == "0.21.17"


def test_latest_skips_unparseable_entries():
    """One weird string on PyPI must not blind the whole check."""
    # Arrange
    releases = ["0.21.14", "not-a-version", "0.21.17"]

    # Act
    result = latest(releases)

    # Assert
    assert result == "0.21.17"


def test_latest_of_nothing_is_none():
    # Arrange
    # Act
    result = latest([])

    # Assert
    assert result is None


# EOF
