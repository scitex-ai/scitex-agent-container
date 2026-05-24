"""Tests for the dead-session resume-failure classifier.

No mocks: every test exercises the real ``_dead_session`` helpers against
plain strings / exceptions. AAA structure, one assertion per test,
descriptive names.
"""

from __future__ import annotations

from scitex_agent_container._runners import _dead_session as ds


def test_is_dead_session_resume_matches_canonical_sdk_phrase() -> None:
    # Arrange
    msg = "Error: No conversation found with session ID: abc-123"
    # Act
    matched = ds.is_dead_session_resume(msg)
    # Assert
    assert matched is True


def test_is_dead_session_resume_is_case_insensitive() -> None:
    # Arrange
    msg = "NO CONVERSATION FOUND WITH SESSION ID: ABC-123"
    # Act
    matched = ds.is_dead_session_resume(msg)
    # Assert
    assert matched is True


def test_is_dead_session_resume_rejects_auth_failure_text() -> None:
    # Arrange — a 401 must NOT be misclassified as a dead session.
    msg = "API error: 401 Unauthorized"
    # Act
    matched = ds.is_dead_session_resume(msg)
    # Assert
    assert matched is False


def test_is_dead_session_resume_rejects_generic_network_crash() -> None:
    # Arrange
    msg = "Connection reset by peer"
    # Act
    matched = ds.is_dead_session_resume(msg)
    # Assert
    assert matched is False


def test_extract_dead_session_id_recovers_uuid_from_message() -> None:
    # Arrange
    msg = "No conversation found with session ID: 6ef8248f-1ccd-4877-934d-908e15333b52"
    # Act
    extracted = ds.extract_dead_session_id(msg)
    # Assert
    assert extracted == "6ef8248f-1ccd-4877-934d-908e15333b52"


def test_extract_dead_session_id_returns_none_when_no_id_present() -> None:
    # Arrange — dead-session phrasing without a parseable uuid.
    msg = "No conversation found for session"
    # Act
    extracted = ds.extract_dead_session_id(msg)
    # Assert
    assert extracted is None
