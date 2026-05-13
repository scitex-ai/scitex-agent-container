"""Tests for config._parsers._claude.parse_claude."""

from __future__ import annotations

from scitex_agent_container.config._parsers._claude import parse_claude


def test_missing_returns_defaults():
    c = parse_claude({})
    assert c.model == ""
    assert c.channels == []
    assert c.flags == []
    assert c.session == "continue"
    assert c.continue_max_age_minutes is None
    assert c.resume_id == ""
    assert c.auto_accept is True
    assert c.raw_options == {}


def test_explicit_none_block_treated_as_empty():
    c = parse_claude({"claude": None})
    assert c.session == "continue"


def test_top_level_session_takes_precedence():
    # `new` is a legacy alias for `new-session` (REQUIREMENT_SUMMARY §3 #6).
    c = parse_claude({"session": "new", "claude": {"session": "continue"}})
    assert c.session == "new-session"


def test_legacy_session_alias_continue_or_new():
    """`continue-or-new` is aliased to `continue` (safe-fallback semantics)."""
    c = parse_claude({"claude": {"session": "continue-or-new"}})
    assert c.session == "continue"


def test_nested_session_used_when_top_absent():
    c = parse_claude({"claude": {"session": "continue"}})
    assert c.session == "continue"


def test_continue_max_age_coerced_to_int():
    c = parse_claude({"claude": {"continue_max_age_minutes": "30"}})
    assert c.continue_max_age_minutes == 30


def test_continue_max_age_invalid_becomes_none():
    c = parse_claude({"claude": {"continue_max_age_minutes": "abc"}})
    assert c.continue_max_age_minutes is None


def test_raw_options_passes_through_dict():
    c = parse_claude({"claude": {"raw_options": {"k": "v"}}})
    assert c.raw_options == {"k": "v"}


def test_raw_options_non_dict_yields_empty():
    c = parse_claude({"claude": {"raw_options": "garbage"}})
    assert c.raw_options == {}


def test_model_channels_flags_resume_id_passthrough():
    c = parse_claude(
        {
            "claude": {
                "model": "opus[1m]",
                "channels": ["beta"],
                "flags": ["--foo"],
                "resume_id": "abc-123",
                "auto_accept": False,
            }
        }
    )
    assert c.model == "opus[1m]"
    assert c.channels == ["beta"]
    assert c.flags == ["--foo"]
    assert c.resume_id == "abc-123"
    assert c.auto_accept is False
