"""Tests for ``config._parsers._skills.parse_skills``.

Each test pins exactly one observable behaviour of the parser. The
``spec.skills`` block is optional and accepts three shapes that all
collapse to a :class:`SkillsSpec`: missing or ``None`` yields documented
defaults; a dict carries ``required``/``available`` lists verbatim;
``injection_mode``, ``match_by`` and ``match_style`` are validated
against fixed enumerations with documented fallback values
(``at-import``, ``["skill-id", "tag"]``, ``exact``). ``injection_mode``
and ``match_style`` strip surrounding whitespace before validation.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._skills import parse_skills

# ---------------------------------------------------------------------------
# Missing skills block -> default SkillsSpec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("required", []),
        ("available", []),
        ("injection_mode", "at-import"),
        ("match_by", ["skill-id", "tag"]),
        ("match_style", "exact"),
    ],
)
def test_missing_skills_block_yields_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_skills(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Explicit None -> treated as empty dict (same defaults)
# ---------------------------------------------------------------------------


def test_explicit_none_skills_yields_default_injection_mode():
    # Arrange
    spec = {"skills": None}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.injection_mode == "at-import"


# ---------------------------------------------------------------------------
# injection_mode validation
# ---------------------------------------------------------------------------


def test_unknown_injection_mode_falls_back_to_at_import():
    # Arrange
    spec = {"skills": {"injection_mode": "weird"}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.injection_mode == "at-import"


def test_block_injection_mode_is_accepted():
    # Arrange
    spec = {"skills": {"injection_mode": "block"}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.injection_mode == "block"


def test_injection_mode_strips_surrounding_whitespace():
    # Arrange
    spec = {"skills": {"injection_mode": "  block  "}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.injection_mode == "block"


# ---------------------------------------------------------------------------
# match_by validation
# ---------------------------------------------------------------------------


def test_match_by_drops_unknown_strategies_preserving_order():
    # Arrange
    spec = {"skills": {"match_by": ["skill-id", "bogus", "filename"]}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.match_by == ["skill-id", "filename"]


def test_match_by_with_only_unknown_strategies_falls_back_to_default():
    # Arrange
    spec = {"skills": {"match_by": ["bogus", "junk"]}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.match_by == ["skill-id", "tag"]


def test_match_by_explicit_empty_list_falls_back_to_default():
    # Arrange
    spec = {"skills": {"match_by": []}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.match_by == ["skill-id", "tag"]


# ---------------------------------------------------------------------------
# match_style validation
# ---------------------------------------------------------------------------


def test_unknown_match_style_falls_back_to_exact():
    # Arrange
    spec = {"skills": {"match_style": "loose"}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.match_style == "exact"


def test_partial_match_style_is_accepted():
    # Arrange
    spec = {"skills": {"match_style": "partial"}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.match_style == "partial"


# ---------------------------------------------------------------------------
# required / available pass-through
# ---------------------------------------------------------------------------


def test_required_list_is_passed_through_verbatim():
    # Arrange
    spec = {"skills": {"required": ["a"], "available": ["b", "c"]}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.required == ["a"]


def test_available_list_is_passed_through_verbatim():
    # Arrange
    spec = {"skills": {"required": ["a"], "available": ["b", "c"]}}
    # Act
    result = parse_skills(spec)
    # Assert
    assert result.available == ["b", "c"]
