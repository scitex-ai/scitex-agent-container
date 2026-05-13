"""Tests for ``config._parsers._a2a.parse_a2a`` (auto-allocation defaults)."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._a2a import parse_a2a


def test_unset_defaults_to_auto():
    """No a2a block at all → port='auto' (the new auto-allocation default)."""
    s = parse_a2a({})
    assert s.port == "auto"
    assert s.is_auto is True
    assert s.is_disabled is False
    assert s.host == "127.0.0.1"


def test_empty_a2a_block_defaults_to_auto():
    """``a2a: {}`` → still port='auto' (key absent inside the block)."""
    s = parse_a2a({"a2a": {}})
    assert s.port == "auto"


def test_explicit_auto_string_parses_to_auto():
    s = parse_a2a({"a2a": {"port": "auto"}})
    assert s.port == "auto"
    assert s.is_auto is True


def test_explicit_int_parses_verbatim():
    s = parse_a2a({"a2a": {"port": 7901}})
    assert s.port == 7901
    assert s.is_auto is False
    assert s.is_disabled is False


def test_explicit_null_disables_sidecar():
    s = parse_a2a({"a2a": {"port": None}})
    assert s.port is None
    assert s.is_auto is False
    assert s.is_disabled is True


def test_unknown_string_raises():
    with pytest.raises(ValueError, match="unknown string"):
        parse_a2a({"a2a": {"port": "random"}})


def test_host_override():
    s = parse_a2a({"a2a": {"host": "0.0.0.0", "port": 8000}})
    assert s.host == "0.0.0.0"
    assert s.port == 8000
