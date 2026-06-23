"""Tests for ``config._parsers._hosts`` (parse_hosts_spec + parse_scheduling).

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), and asserts exactly one
fact (TQ007). Shared-setup invariants collapse into
``pytest.parametrize`` so the matrix stays declarative.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._hosts import (
    parse_hosts_spec,
    parse_scheduling,
)

# ---------------------------------------------------------------------------
# parse_hosts_spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,field,expected",
    [
        # Empty spec → both fields empty strings.
        ({}, "host", ""),
        ({}, "hosts", ""),
        # ``host`` as scalar string → host=str, hosts="".
        ({"host": "alpha"}, "host", "alpha"),
        ({"host": "alpha"}, "hosts", ""),
        # ``host`` explicitly None → treated as empty (local singleton).
        ({"host": None}, "host", ""),
        ({"host": None}, "hosts", ""),
        # ``hosts: "all"`` keyword → host="", hosts="all".
        ({"hosts": "all"}, "host", ""),
        ({"hosts": "all"}, "hosts", "all"),
        # Unsupported scalar type for ``host`` → normalized to empty.
        ({"host": 42}, "host", ""),
        # ``host: local`` → the EXPLICIT local-singleton spelling, normalized
        # to "" so downstream placement treats it as the historical default
        # (never SSH-dispatched to a peer literally named "local").
        ({"host": "local"}, "host", ""),
        ({"host": "local"}, "hosts", ""),
        # Case-insensitive + whitespace-tolerant.
        ({"host": "LOCAL"}, "host", ""),
        ({"host": "  local  "}, "host", ""),
    ],
)
def test_parse_hosts_spec_returns_expected_field_for_spec(spec, field, expected):
    # Arrange — input dict provided by parametrize.
    # Act
    parsed = parse_hosts_spec(spec)
    # Assert
    assert getattr(parsed, field) == expected


def test_parse_hosts_spec_normalises_host_list_entries_to_strings():
    # Arrange — mixed-type list under ``host``.
    spec = {"host": ["a", 1, "b"]}
    # Act
    parsed = parse_hosts_spec(spec)
    # Assert — every entry stringified, order preserved.
    assert parsed.host == ["a", "1", "b"]


def test_parse_hosts_spec_returns_list_for_hosts_list_input():
    # Arrange
    spec = {"hosts": ["x", "y"]}
    # Act
    parsed = parse_hosts_spec(spec)
    # Assert
    assert parsed.hosts == ["x", "y"]


# ---------------------------------------------------------------------------
# parse_scheduling
# ---------------------------------------------------------------------------


def test_parse_scheduling_absent_key_returns_explicit_false_flag():
    # Arrange — spec has no ``scheduling`` key.
    spec: dict = {}
    # Act
    _, explicit = parse_scheduling(spec)
    # Assert
    assert explicit is False


def test_parse_scheduling_absent_key_uses_default_per_host_mode():
    # Arrange
    spec: dict = {}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.mode == "per-host"


def test_parse_scheduling_absent_key_uses_empty_preferred_host():
    # Arrange
    spec: dict = {}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.preferred_host == ""


def test_parse_scheduling_absent_key_uses_empty_fallback_hosts_list():
    # Arrange
    spec: dict = {}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.fallback_hosts == []


def test_parse_scheduling_present_block_marks_explicit_flag_true():
    # Arrange — scheduling block present, mode set.
    spec = {"scheduling": {"mode": "singleton"}}
    # Act
    _, explicit = parse_scheduling(spec)
    # Assert
    assert explicit is True


def test_parse_scheduling_present_block_uses_caller_supplied_mode():
    # Arrange
    spec = {"scheduling": {"mode": "singleton"}}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.mode == "singleton"


def test_parse_scheduling_empty_block_marks_explicit_flag_true():
    # Arrange — empty mapping under ``scheduling``.
    spec: dict = {"scheduling": {}}
    # Act
    _, explicit = parse_scheduling(spec)
    # Assert
    assert explicit is True


def test_parse_scheduling_empty_block_defaults_mode_to_per_host():
    # Arrange
    spec: dict = {"scheduling": {}}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.mode == "per-host"


def test_parse_scheduling_invalid_mode_value_raises_value_error():
    # Arrange — ``swarm`` is not a recognized scheduling mode.
    spec = {"scheduling": {"mode": "swarm"}}
    # Act
    call = lambda: parse_scheduling(spec)
    # Assert
    with pytest.raises(ValueError, match="mode"):
        call()


def test_parse_scheduling_non_mapping_block_raises_value_error():
    # Arrange — list is not a mapping.
    spec = {"scheduling": ["a"]}
    # Act
    call = lambda: parse_scheduling(spec)
    # Assert
    with pytest.raises(ValueError, match="mapping"):
        call()


def test_parse_scheduling_hyphen_form_extracts_preferred_host_value():
    # Arrange — hyphen-cased keys (YAML-native form).
    spec = {"scheduling": {"preferred-host": "node1", "fallback-hosts": ["n2", "n3"]}}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.preferred_host == "node1"


def test_parse_scheduling_hyphen_form_extracts_fallback_hosts_list():
    # Arrange
    spec = {"scheduling": {"preferred-host": "node1", "fallback-hosts": ["n2", "n3"]}}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.fallback_hosts == ["n2", "n3"]


def test_parse_scheduling_underscore_form_extracts_preferred_host_value():
    # Arrange — underscore-cased keys (Python-native form).
    spec = {
        "scheduling": {
            "preferred_host": "node-u",
            "fallback_hosts": "single-fallback",
        }
    }
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.preferred_host == "node-u"


def test_parse_scheduling_underscore_form_lifts_string_fallback_into_list():
    # Arrange — bare string under ``fallback_hosts``.
    spec = {
        "scheduling": {
            "preferred_host": "node-u",
            "fallback_hosts": "single-fallback",
        }
    }
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert — single string lifted into a one-element list.
    assert sched.fallback_hosts == ["single-fallback"]


def test_parse_scheduling_coerces_fallback_host_entries_to_strings():
    # Arrange — mixed-type list of fallback hosts.
    spec = {"scheduling": {"fallback_hosts": [1, "two", 3.0]}}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.fallback_hosts == ["1", "two", "3.0"]


def test_parse_scheduling_empty_list_block_raises_value_error():
    # Arrange — falsy non-dict (empty list) must not be silently coerced to {}.
    spec = {"scheduling": []}
    # Act
    call = lambda: parse_scheduling(spec)
    # Assert
    with pytest.raises(ValueError, match="mapping"):
        call()


def test_parse_scheduling_none_block_marks_explicit_flag_true():
    # Arrange — ``scheduling: null`` in YAML lands as None here.
    spec = {"scheduling": None}
    # Act
    _, explicit = parse_scheduling(spec)
    # Assert
    assert explicit is True


def test_parse_scheduling_none_block_defaults_mode_to_per_host():
    # Arrange
    spec = {"scheduling": None}
    # Act
    sched, _ = parse_scheduling(spec)
    # Assert
    assert sched.mode == "per-host"
