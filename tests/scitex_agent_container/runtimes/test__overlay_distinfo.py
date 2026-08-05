"""Restart-collision prediction for overlay agents — the whiteout is name-specific.

Mirrors ``src/scitex_agent_container/runtimes/_overlay_distinfo.py``.

The load-bearing property is that a whiteout subtracts EXACTLY ONE NAME. Every
other behaviour here follows from it, including the one that makes the bug
dangerous: a whiteout spelling a version the NEW base does not contain removes
nothing, so the new base's ``.dist-info`` survives alongside the overlay's and
the distribution refuses at boot.

The important assertion is the CONTRAST between
``test_uninstalled_under_old_base_collides_after_restart`` and
``test_uninstalled_under_current_base_is_restart_safe``. They are built from a
real 2026-07-28 measurement of two agents holding the SAME package version over
the SAME base image, both healthy, predicting OPPOSITE outcomes at next
restart. They are split only because one assertion per test is required — read
them together: a prediction that could not separate those two would be
worthless exactly where it is needed.

Pure name algebra, so no device nodes, no root and no apptainer are required to
exercise the rule that encodes the bug (PA-306: no mocks — there is nothing to
mock, the functions take plain collections).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._overlay_distinfo import (
    find_collisions,
    parse_dist_info,
    predict_merged_names,
    predict_restart_collisions,
)

# The base image both measured agents were sitting on.
BASE_0_17_9 = ["scitex_cards-0.17.9.dist-info", "pyyaml-6.0.2.dist-info"]

# Both agents held this same overlay copy and were both healthy at the time.
OVERLAY_0_17_10 = ["scitex_cards-0.17.10.dist-info"]

# The only difference between them: which base was mounted when each ran its
# in-container install, i.e. which name its whiteout spells.
WHITEOUTS_UNINSTALLED_UNDER_OLD_BASE = [
    "scitex_cards-0.17.5.dist-info",
    "scitex_cards-0.17.7.dist-info",
]
WHITEOUTS_UNINSTALLED_UNDER_CURRENT_BASE = [
    "scitex_cards-0.17.7.dist-info",
    "scitex_cards-0.17.9.dist-info",
]


def test_parse_dist_info_splits_distribution_from_version() -> None:
    # Arrange
    name = "scitex_cards-0.17.9.dist-info"
    # Act
    parsed = parse_dist_info(name)
    # Assert
    assert parsed == ("scitex_cards", "0.17.9")


@pytest.mark.parametrize(
    "name",
    ["scitex_cards", "scitex_cards-0.17.9.egg-info", "README.md", ""],
)
def test_parse_dist_info_returns_none_for_non_dist_info(name: str) -> None:
    """Callers hand over raw listings, so non-matches must not raise."""
    # Arrange
    candidate = name
    # Act
    parsed = parse_dist_info(candidate)
    # Assert
    assert parsed is None


def test_whiteout_removes_the_base_name_it_spells() -> None:
    # Arrange
    whiteouts = ["scitex_cards-0.17.9.dist-info"]
    # Act
    merged = predict_merged_names(BASE_0_17_9, OVERLAY_0_17_10, whiteouts)
    # Assert
    assert "scitex_cards-0.17.9.dist-info" not in merged


def test_overlay_copy_is_visible_in_the_merged_view() -> None:
    # Arrange
    whiteouts = ["scitex_cards-0.17.9.dist-info"]
    # Act
    merged = predict_merged_names(BASE_0_17_9, OVERLAY_0_17_10, whiteouts)
    # Assert
    assert "scitex_cards-0.17.10.dist-info" in merged


def test_uninstalled_under_old_base_collides_after_restart() -> None:
    """THE BUG — the whiteout names a version the NEW base does not carry.

    Nothing re-evaluates the whiteout when the base changes, so it subtracts
    nothing and the new base's copy survives beside the overlay's.
    """
    # Arrange
    whiteouts = WHITEOUTS_UNINSTALLED_UNDER_OLD_BASE
    # Act
    collisions = predict_restart_collisions(BASE_0_17_9, OVERLAY_0_17_10, whiteouts)
    # Assert
    assert collisions == {"scitex_cards": ["0.17.10", "0.17.9"]}


def test_uninstalled_under_current_base_is_restart_safe() -> None:
    """The CONTROL — same version, same base, whiteout covers the base copy."""
    # Arrange
    whiteouts = WHITEOUTS_UNINSTALLED_UNDER_CURRENT_BASE
    # Act
    collisions = predict_restart_collisions(BASE_0_17_9, OVERLAY_0_17_10, whiteouts)
    # Assert
    assert collisions == {}


def test_overlay_holding_the_same_version_as_base_does_not_collide() -> None:
    """Identical names are ONE directory in the merged view, not two."""
    # Arrange
    overlay_real = ["scitex_cards-0.17.9.dist-info"]
    # Act
    collisions = predict_restart_collisions(BASE_0_17_9, overlay_real, [])
    # Assert
    assert collisions == {}


def test_overlay_copy_with_no_whiteout_at_all_collides() -> None:
    """An overlay that never uninstalled leaves the base copy fully exposed."""
    # Arrange
    overlay_real = ["scitex_cards-0.17.5.dist-info"]
    # Act
    collisions = predict_restart_collisions(BASE_0_17_9, overlay_real, [])
    # Assert
    assert collisions == {"scitex_cards": ["0.17.5", "0.17.9"]}


def test_reconciled_overlay_predicts_no_collision() -> None:
    """The repaired state: the base shows through untouched."""
    # Arrange
    reconciled: list[str] = []
    # Act
    collisions = predict_restart_collisions(BASE_0_17_9, reconciled, reconciled)
    # Assert
    assert collisions == {}


def test_unrelated_distributions_are_not_conflated() -> None:
    # Arrange
    names = [
        "scitex_cards-0.17.9.dist-info",
        "scitex_cards-0.17.10.dist-info",
        "pyyaml-6.0.2.dist-info",
    ]
    # Act
    collisions = find_collisions(names)
    # Assert
    assert set(collisions) == {"scitex_cards"}


def test_prerelease_and_local_versions_stay_one_distribution() -> None:
    """A local/pre-release segment must not read as a different distribution."""
    # Arrange
    names = [
        "scitex_cards-1.2.3+local.dist-info",
        "scitex_cards-2.0.0rc1.dist-info",
    ]
    # Act
    collisions = find_collisions(names)
    # Assert
    assert collisions == {"scitex_cards": ["1.2.3+local", "2.0.0rc1"]}
