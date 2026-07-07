"""``spec.claude.resume_id`` UUID validation (extracted _claude_validation).

A non-empty ``resume_id`` MUST be a well-formed UUID: the SDK/CLI resume
mechanism silently discards an unknown/malformed id to a fresh session,
so a typo would degrade the pin invisibly. The validator fails loud
naming the bad value. Real ``validate_claude`` on real dicts, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._claude_validation import validate_claude

_VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_valid_uuid_resume_id_produces_no_error():
    # Arrange
    spec = {"claude": {"model": "haiku", "resume_id": _VALID_UUID}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert errors == []


def test_malformed_resume_id_is_rejected():
    # Arrange
    spec = {"claude": {"model": "haiku", "resume_id": "not-a-uuid"}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert any("resume_id" in e and "not a valid UUID" in e for e in errors)


def test_malformed_resume_id_error_names_the_bad_value():
    # Arrange
    spec = {"claude": {"model": "haiku", "resume_id": "bogus-123"}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert any("bogus-123" in e for e in errors)


def test_empty_resume_id_produces_no_error():
    # Arrange — empty means "not pinned"; nothing to validate.
    spec = {"claude": {"model": "haiku", "resume_id": ""}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert errors == []


def test_absent_resume_id_produces_no_error():
    # Arrange
    spec = {"claude": {"model": "haiku"}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert errors == []


def test_non_string_resume_id_is_rejected():
    # Arrange
    spec = {"claude": {"model": "haiku", "resume_id": 12345}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert any("resume_id must be a string" in e for e in errors)
