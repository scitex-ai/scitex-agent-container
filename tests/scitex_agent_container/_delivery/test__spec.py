"""The spec table's guardrails — MUTATION-PROVED, not merely called.

Each invariant is driven with a table built to VIOLATE it, so these tests would
go red if the check were deleted. A validator nobody has watched reject anything
is a hope with a function signature around it.
"""

from __future__ import annotations

from functools import partial

import pytest

from scitex_agent_container._delivery._spec import (
    DELIVERY_SIGNALS,
    OBSERVATION_DIRECT,
    OBSERVATION_INFERRED,
    DeliverySignalSpec,
    delivery_spec_for,
    validate_delivery_specs,
)


def _spec(name, **kwargs):
    base = dict(
        name=name,
        reads="whatever",
        healthy=True,
        load_bearing=True,
        why="test fixture",
    )
    base.update(kwargs)
    return DeliverySignalSpec(**base)


def test_shipped_table_validates_at_import():
    # Arrange
    table = DELIVERY_SIGNALS
    # Act
    result = validate_delivery_specs(table)
    # Assert
    assert result is None


def test_no_shipped_signal_is_decisive():
    # Arrange
    table = DELIVERY_SIGNALS
    # Act
    decisive = [s.name for s in table if s.decisive]
    # Assert
    assert decisive == []


def test_duplicate_signal_name_is_rejected():
    # Arrange
    bad = (_spec("is_route_resolved"), _spec("is_route_resolved"))
    # Act
    validating = partial(validate_delivery_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="duplicate delivery signal"):
        validating()


def test_decisive_inferred_signal_is_rejected():
    # Arrange
    bad = (_spec("x", decisive=True, observation=OBSERVATION_INFERRED),)
    # Act
    validating = partial(validate_delivery_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="DECISIVE REQUIRES DIRECT OBSERVATION"):
        validating()


def test_decisive_non_load_bearing_is_rejected():
    # Arrange
    bad = (
        _spec("x", decisive=True, load_bearing=False, observation=OBSERVATION_DIRECT),
    )
    # Act
    validating = partial(validate_delivery_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="cannot short-circuit"):
        validating()


def test_unknown_signal_name_raises_keyerror():
    # Arrange
    name = "is_totally_invented"
    # Act
    looking_up = partial(delivery_spec_for, name)
    # Assert
    with pytest.raises(KeyError, match="unknown delivery signal"):
        looking_up()


def test_three_signals_are_load_bearing():
    # Arrange
    table = DELIVERY_SIGNALS
    # Act
    load_bearing = [s.name for s in table if s.load_bearing]
    # Assert
    assert load_bearing == [
        "is_route_resolved",
        "is_payload_delivered",
        "is_payload_submitted",
    ]
