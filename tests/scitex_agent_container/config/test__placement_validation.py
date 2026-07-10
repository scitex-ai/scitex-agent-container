"""``spec.host`` / ``spec.hosts`` placement validation.

Exactly one of host (singleton) / hosts (multi-instance) is REQUIRED and
the two are mutually exclusive. ``host: local`` / ``localhost`` are BANNED
(operator directive 2026-07-10 — placement carries the RESOLVED hostname).
Real ``validate_placement`` on real dicts, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._placement_validation import validate_placement


def test_neither_host_nor_hosts_is_rejected():
    # Arrange — no placement declared at all.
    spec: dict = {}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("REQUIRED" in e for e in errors)


def test_missing_placement_hint_does_not_suggest_banned_local():
    # Arrange — the REQUIRED-error fix hint must steer to resolved names,
    # never to the banned relative spelling.
    spec: dict = {}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert "host: local" not in "\n".join(errors)


def test_both_host_and_hosts_is_rejected_as_mutually_exclusive():
    # Arrange
    spec = {"host": "gpu-box", "hosts": ["a"]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("mutually exclusive" in e for e in errors)


def test_singleton_host_string_produces_no_error():
    # Arrange — a concrete resolved hostname is the doctrine-conformant form.
    spec = {"host": "ywata-note-win"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert errors == []


def test_hostname_placeholder_singleton_produces_no_error():
    # Arrange — ${HOSTNAME} is the portable spelling; the loader resolves it.
    spec = {"host": "${HOSTNAME}"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert errors == []


@pytest.mark.parametrize("banned", ["local", "localhost", "LOCAL", "  local  "])
def test_relative_host_spelling_is_banned(banned):
    # Arrange — relative "this machine" spellings, any case/padding.
    spec = {"host": banned}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("BANNED" in e for e in errors)


def test_banned_host_error_carries_migration_hint():
    # Arrange — the error must tell the operator the exact fix.
    spec = {"host": "local"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("hostname -s" in e for e in errors)


def test_banned_local_inside_host_fallback_chain_is_rejected():
    # Arrange — a fallback chain smuggling 'local' in the tail.
    spec = {"host": ["gpu-box", "local"]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("BANNED" in e for e in errors)


def test_banned_local_inside_hosts_list_is_rejected():
    # Arrange — multi-instance list smuggling 'localhost'.
    spec = {"hosts": ["nas", "localhost"]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("BANNED" in e for e in errors)


def test_host_list_of_non_strings_is_rejected():
    # Arrange
    spec = {"host": ["ok", 123]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("host list must contain only strings" in e for e in errors)


def test_host_wrong_type_is_rejected():
    # Arrange — a mapping is neither a string nor a list.
    spec = {"host": {"peer": "x"}}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("spec.host must be a string" in e for e in errors)


def test_hosts_none_is_rejected_as_empty():
    # Arrange — ``hosts:`` written with no value.
    spec = {"hosts": None}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("cannot be empty" in e for e in errors)


def test_hosts_string_all_produces_no_error():
    # Arrange
    spec = {"hosts": "all"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert errors == []


def test_hosts_string_other_than_all_is_rejected():
    # Arrange
    spec = {"hosts": "everywhere"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("must be 'all'" in e for e in errors)


def test_hosts_list_of_strings_produces_no_error():
    # Arrange
    spec = {"hosts": ["a", "b"]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert errors == []


def test_hosts_list_of_non_strings_is_rejected():
    # Arrange
    spec = {"hosts": ["a", 2]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("hosts list must contain only strings" in e for e in errors)
