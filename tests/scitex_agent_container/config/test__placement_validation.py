"""``spec.host`` / ``spec.hosts`` placement validation.

Exactly one of host (singleton) / hosts (multi-instance) is REQUIRED and
the two are mutually exclusive. Real ``validate_placement`` on real
dicts, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._placement_validation import validate_placement


def test_neither_host_nor_hosts_is_rejected():
    # Arrange — no placement declared at all.
    spec: dict = {}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("REQUIRED" in e for e in errors)


def test_both_host_and_hosts_is_rejected_as_mutually_exclusive():
    # Arrange
    spec = {"host": "local", "hosts": ["a"]}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert any("mutually exclusive" in e for e in errors)


def test_singleton_host_string_produces_no_error():
    # Arrange
    spec = {"host": "local"}
    # Act
    errors = validate_placement(spec)
    # Assert
    assert errors == []


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
