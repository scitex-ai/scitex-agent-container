"""Tests for config._parsers._remote.parse_remote."""

from __future__ import annotations

from scitex_agent_container.config._parsers._remote import parse_remote


def test_missing_returns_empty_default():
    r = parse_remote({})
    assert r.hops == []
    assert r.host == ""
    assert r.user == ""
    assert r.key == ""
    assert r.port == 22
    assert r.login_shell is True
    assert r.no_preflight is False


def test_explicit_none_treated_as_empty_dict():
    r = parse_remote({"remote": None})
    assert r.host == ""
    assert r.hops == []


def test_list_input_builds_hop_chain():
    r = parse_remote({"remote": ["bastion", "node-7"]})
    assert r.hops == ["bastion", "node-7"]
    # host left empty because explicit chain was given.
    assert r.host == ""


def test_list_input_drops_falsy_entries():
    r = parse_remote({"remote": ["a", "", None, "b"]})
    assert r.hops == ["a", "b"]


def test_string_input_creates_single_hop_and_host():
    r = parse_remote({"remote": "edgenode"})
    assert r.hops == ["edgenode"]
    assert r.host == "edgenode"


def test_string_whitespace_only_gives_no_hop():
    r = parse_remote({"remote": "   "})
    # whitespace → no hop, host is the stripped empty string
    assert r.hops == []
    assert r.host == ""


def test_dict_form_legacy_fields():
    r = parse_remote(
        {
            "remote": {
                "host": "h",
                "user": "u",
                "key": "/k",
                "port": "2222",
                "login_shell": False,
                "no_preflight": True,
            }
        }
    )
    assert r.host == "h"
    assert r.user == "u"
    assert r.key == "/k"
    assert r.port == 2222
    assert r.login_shell is False
    assert r.no_preflight is True


def test_dict_form_defaults():
    r = parse_remote({"remote": {"host": "h"}})
    assert r.host == "h"
    assert r.user == ""
    assert r.key == ""
    assert r.port == 22
    assert r.login_shell is True
    assert r.no_preflight is False
