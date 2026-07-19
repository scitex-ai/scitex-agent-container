"""``DeliveryState`` refuses the shapes that let an UNKNOWN become a pole."""

from __future__ import annotations

from functools import partial

import pytest

from scitex_agent_container._delivery._state import DeliveryState


def test_default_signals_are_all_none():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    values = set(state.signals().values())
    # Assert
    assert values == {None}


def test_unknown_constructor_reasons_every_signal():
    # Arrange
    state = DeliveryState.unknown("peer", "nobody looked")
    # Act
    reasons = set(state.reasons.values())
    # Assert
    assert reasons == {"nobody looked"}


def test_non_bool_signal_raises_typeerror():
    # Arrange
    building = partial(DeliveryState, agent="peer", is_route_resolved="yes")
    # Act
    constructing = building
    # Assert
    with pytest.raises(TypeError, match="must be True, False or None"):
        constructing()


def test_integer_signal_raises_typeerror_too():
    # Arrange
    building = partial(DeliveryState, agent="peer", is_payload_submitted=1)
    # Act
    constructing = building
    # Assert
    with pytest.raises(TypeError, match="must be True, False or None"):
        constructing()


def test_unknown_reason_key_is_rejected():
    # Arrange
    building = partial(DeliveryState, agent="peer", reasons={"is_not_a_signal": "oops"})
    # Act
    constructing = building
    # Assert
    with pytest.raises(KeyError, match="unknown delivery signal"):
        constructing()


def test_with_signal_records_its_reason():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    updated = state.with_signal("is_route_resolved", True, "session present")
    # Assert
    assert updated.reason_for("is_route_resolved") == "session present"


def test_with_signal_keeps_raw_evidence():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    updated = state.with_signal(
        "is_payload_delivered", True, "token seen", pane_after_paste="❯ hi\n"
    )
    # Assert
    assert updated.raw["pane_after_paste"] == "❯ hi\n"


def test_with_signal_leaves_original_untouched():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    state.with_signal("is_route_resolved", True, "session present")
    # Assert
    assert state.is_route_resolved is None


def test_signals_always_returns_full_set():
    # Arrange
    state = DeliveryState(agent="peer").with_signal("is_route_resolved", True)
    # Act
    names = sorted(state.signals())
    # Assert
    assert len(names) == 6


def test_to_dict_carries_the_raw_captures():
    # Arrange
    state = DeliveryState(agent="peer").with_signal(
        "is_payload_delivered", False, "no token", pane_after_paste="❯\n"
    )
    # Act
    payload = state.to_dict()
    # Assert
    assert payload["raw"]["pane_after_paste"] == "❯\n"


def test_to_dict_reports_a_false_signal():
    # Arrange
    state = DeliveryState(agent="peer").with_signal(
        "is_payload_submitted", False, "still pending"
    )
    # Act
    payload = state.to_dict()
    # Assert
    assert payload["signals"]["is_payload_submitted"]["value"] is False
