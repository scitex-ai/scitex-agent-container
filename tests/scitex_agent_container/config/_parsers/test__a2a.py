"""Tests for ``config._parsers._a2a.parse_a2a`` (auto-allocation defaults).

The parser maps ``spec.a2a`` into an ``A2ASpec`` with the following
port semantics: an absent block or absent ``port`` key collapses to
``port="auto"`` (auto-allocation); ``"auto"`` is round-tripped; an
integer is cast verbatim; ``None`` explicitly disables the sidecar;
any unknown string raises ``ValueError`` rather than silently falling
back. The optional ``host`` field defaults to ``127.0.0.1`` and is
otherwise round-tripped.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._a2a import parse_a2a

# ---------------------------------------------------------------------------
# No a2a block at all → auto-allocation default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("port", "auto"),
        ("is_auto", True),
        ("is_disabled", False),
        ("host", "127.0.0.1"),
    ],
)
def test_missing_a2a_block_yields_auto_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Empty a2a block (key present, port key absent) → auto-allocation default
# ---------------------------------------------------------------------------


def test_empty_a2a_block_defaults_port_to_auto():
    # Arrange
    spec = {"a2a": {}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert result.port == "auto"


# ---------------------------------------------------------------------------
# Explicit ``port: auto`` string → auto sentinel
# ---------------------------------------------------------------------------


def test_explicit_auto_string_yields_auto_port():
    # Arrange
    spec = {"a2a": {"port": "auto"}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert result.port == "auto"


def test_explicit_auto_string_sets_is_auto_flag():
    # Arrange
    spec = {"a2a": {"port": "auto"}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert result.is_auto is True


# ---------------------------------------------------------------------------
# Explicit integer port → operator-pinned value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("port", 7901),
        ("is_auto", False),
        ("is_disabled", False),
    ],
)
def test_explicit_int_port_round_trips_field(attr, expected):
    # Arrange
    spec = {"a2a": {"port": 7901}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Explicit ``port: null`` → sidecar disabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("port", None),
        ("is_auto", False),
        ("is_disabled", True),
    ],
)
def test_explicit_null_port_disables_sidecar_field(attr, expected):
    # Arrange
    spec = {"a2a": {"port": None}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert getattr(result, attr) == expected


# ---------------------------------------------------------------------------
# Unknown string → ValueError (no silent fallback)
# ---------------------------------------------------------------------------


def test_unknown_string_port_raises_value_error():
    # Arrange
    spec = {"a2a": {"port": "random"}}
    # Act
    ctx = pytest.raises(ValueError, match="unknown string")
    # Assert
    with ctx:
        parse_a2a(spec)


# ---------------------------------------------------------------------------
# Host override → round-tripped alongside explicit port
# ---------------------------------------------------------------------------


def test_host_override_round_trips_host_field():
    # Arrange
    spec = {"a2a": {"host": "0.0.0.0", "port": 8000}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert result.host == "0.0.0.0"


def test_host_override_preserves_explicit_port():
    # Arrange
    spec = {"a2a": {"host": "0.0.0.0", "port": 8000}}
    # Act
    result = parse_a2a(spec)
    # Assert
    assert result.port == 8000
