"""Tests for ``config._parsers._remote.parse_remote``.

Each test pins exactly one observable behaviour of the parser. The
``spec.remote`` block is optional and accepts three shapes: missing /
``None`` collapses to an empty ``RemoteSpec`` with documented defaults;
a list of SSH-config aliases becomes a hop chain (falsy entries
dropped, ``host`` left empty); a single string becomes both a one-hop
chain and the legacy ``host`` field (whitespace-only strings produce
no hop and an empty host); a dict carries the legacy
``host/user/key/port/login_shell/no_preflight`` fields with documented
defaults when individual keys are omitted.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._remote import parse_remote

# ---------------------------------------------------------------------------
# Missing remote block → default RemoteSpec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("hops", []),
        ("host", ""),
        ("user", ""),
        ("key", ""),
        ("port", 22),
        ("login_shell", True),
        ("no_preflight", False),
    ],
)
def test_missing_remote_block_yields_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_remote(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Explicit None → treated as empty dict (same defaults)
# ---------------------------------------------------------------------------


def test_explicit_none_remote_yields_empty_host():
    # Arrange
    spec = {"remote": None}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.host == ""


def test_explicit_none_remote_yields_empty_hops():
    # Arrange
    spec = {"remote": None}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.hops == []


# ---------------------------------------------------------------------------
# List input → hop chain (falsy entries dropped, host left empty)
# ---------------------------------------------------------------------------


def test_list_input_builds_hop_chain_in_order():
    # Arrange
    spec = {"remote": ["bastion", "node-7"]}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.hops == ["bastion", "node-7"]


def test_list_input_leaves_host_empty_because_chain_was_explicit():
    # Arrange
    spec = {"remote": ["bastion", "node-7"]}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.host == ""


def test_list_input_drops_empty_and_none_entries():
    # Arrange
    spec = {"remote": ["a", "", None, "b"]}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.hops == ["a", "b"]


# ---------------------------------------------------------------------------
# String input → single-hop chain plus legacy host field
# ---------------------------------------------------------------------------


def test_string_input_creates_single_hop():
    # Arrange
    spec = {"remote": "edgenode"}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.hops == ["edgenode"]


def test_string_input_populates_legacy_host_field():
    # Arrange
    spec = {"remote": "edgenode"}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.host == "edgenode"


def test_whitespace_only_string_yields_no_hop():
    # Arrange
    spec = {"remote": "   "}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.hops == []


def test_whitespace_only_string_yields_empty_host_after_strip():
    # Arrange
    spec = {"remote": "   "}
    # Act
    result = parse_remote(spec)
    # Assert
    assert result.host == ""


# ---------------------------------------------------------------------------
# Dict input → legacy fields are round-tripped, port coerced to int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("host", "h"),
        ("user", "u"),
        ("key", "/k"),
        ("port", 2222),
        ("login_shell", False),
        ("no_preflight", True),
    ],
)
def test_dict_form_legacy_field_is_round_tripped(attr, expected):
    # Arrange
    spec = {
        "remote": {
            "host": "h",
            "user": "u",
            "key": "/k",
            "port": "2222",
            "login_shell": False,
            "no_preflight": True,
        }
    }
    # Act
    result = parse_remote(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Dict input with omitted optional keys → documented defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("host", "h"),
        ("user", ""),
        ("key", ""),
        ("port", 22),
        ("login_shell", True),
        ("no_preflight", False),
    ],
)
def test_dict_form_omitted_field_uses_default(attr, expected):
    # Arrange
    spec = {"remote": {"host": "h"}}
    # Act
    result = parse_remote(spec)
    # Assert
    assert getattr(result, attr) == expected
