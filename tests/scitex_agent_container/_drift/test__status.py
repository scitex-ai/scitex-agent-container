"""Tests for the DriftStatus model.

PA-306: no mocks. Pure dataclass behaviour — real instances, real
assertions. Each test: AAA markers (TQ002), one assertion (TQ007),
3+-word descriptive name (TQ003).
"""

from __future__ import annotations

from scitex_agent_container._drift import DriftState, DriftStatus


def test_current_status_reports_not_drifted():
    # Arrange
    status = DriftStatus(state=DriftState.CURRENT)
    # Act
    drifted = status.is_drifted
    # Assert
    assert drifted is False


def test_behind_status_reports_drifted():
    # Arrange
    status = DriftStatus(state=DriftState.BEHIND, behind=3, upstream="origin/develop")
    # Act
    drifted = status.is_drifted
    # Assert
    assert drifted is True


def test_diverged_status_reports_drifted():
    # Arrange
    status = DriftStatus(state=DriftState.DIVERGED, behind=2, ahead=1)
    # Act
    drifted = status.is_drifted
    # Assert
    assert drifted is True


def test_not_a_repo_status_is_not_treated_as_drift():
    # Arrange
    status = DriftStatus(state=DriftState.NOT_A_REPO)
    # Act
    drifted = status.is_drifted
    # Assert
    assert drifted is False


def test_unreachable_status_is_not_treated_as_drift():
    # Arrange
    status = DriftStatus(state=DriftState.UNREACHABLE, detail="offline")
    # Act
    drifted = status.is_drifted
    # Assert
    assert drifted is False


def test_behind_summary_names_commit_count():
    # Arrange
    status = DriftStatus(state=DriftState.BEHIND, behind=3, upstream="origin/develop")
    # Act
    summary = status.summary()
    # Assert
    assert summary == "3 behind origin/develop"


def test_ahead_summary_marks_unpushed():
    # Arrange
    status = DriftStatus(state=DriftState.AHEAD, ahead=2, upstream="origin/develop")
    # Act
    summary = status.summary()
    # Assert
    assert "unpushed" in summary


def test_diverged_summary_reports_both_directions():
    # Arrange
    status = DriftStatus(
        state=DriftState.DIVERGED, ahead=2, behind=5, upstream="origin/develop"
    )
    # Act
    summary = status.summary()
    # Assert
    assert "2 ahead / 5 behind" in summary


def test_to_dict_carries_state_value_string():
    # Arrange
    status = DriftStatus(state=DriftState.BEHIND, behind=1, upstream="origin/develop")
    # Act
    payload = status.to_dict()
    # Assert
    assert payload["state"] == "behind"


def test_to_dict_includes_human_summary_field():
    # Arrange
    status = DriftStatus(state=DriftState.CURRENT)
    # Act
    payload = status.to_dict()
    # Assert
    assert payload["summary"] == "current"
