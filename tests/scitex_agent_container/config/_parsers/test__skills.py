"""Tests for config._parsers._skills.parse_skills."""

from __future__ import annotations

from scitex_agent_container.config._parsers._skills import parse_skills


def test_missing_returns_defaults():
    s = parse_skills({})
    assert s.required == []
    assert s.available == []
    assert s.injection_mode == "at-import"
    assert s.match_by == ["skill-id", "tag"]
    assert s.match_style == "exact"


def test_explicit_none_treated_as_empty():
    s = parse_skills({"skills": None})
    assert s.injection_mode == "at-import"


def test_injection_mode_invalid_falls_back():
    s = parse_skills({"skills": {"injection_mode": "weird"}})
    assert s.injection_mode == "at-import"


def test_injection_mode_block_allowed():
    s = parse_skills({"skills": {"injection_mode": "block"}})
    assert s.injection_mode == "block"


def test_match_by_filters_unknown_strategies():
    s = parse_skills({"skills": {"match_by": ["skill-id", "bogus", "filename"]}})
    assert s.match_by == ["skill-id", "filename"]


def test_match_by_all_invalid_falls_back():
    s = parse_skills({"skills": {"match_by": ["bogus", "junk"]}})
    assert s.match_by == ["skill-id", "tag"]


def test_match_by_explicit_empty_falls_back():
    s = parse_skills({"skills": {"match_by": []}})
    assert s.match_by == ["skill-id", "tag"]


def test_match_style_validated():
    s = parse_skills({"skills": {"match_style": "loose"}})
    assert s.match_style == "exact"
    s2 = parse_skills({"skills": {"match_style": "partial"}})
    assert s2.match_style == "partial"


def test_required_and_available_passthrough():
    s = parse_skills({"skills": {"required": ["a"], "available": ["b", "c"]}})
    assert s.required == ["a"]
    assert s.available == ["b", "c"]


def test_injection_mode_strips_whitespace():
    s = parse_skills({"skills": {"injection_mode": "  block  "}})
    assert s.injection_mode == "block"
