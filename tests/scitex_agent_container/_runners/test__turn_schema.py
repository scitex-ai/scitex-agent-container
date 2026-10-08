from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from scitex_agent_container._runners._turn_schema import TurnRequest


def test_turn_request_preserves_valid_routing_identity():
    # Arrange
    payload = {
        "text": "steer now",
        "exit_after": False,
        "dispatch_id": "dispatch-1",
        "from_agent": "scitex-cards",
    }
    # Act
    request = TurnRequest.model_validate(payload)

    # Assert
    assert (request.dispatch_id, request.from_agent) == (
        "dispatch-1",
        "scitex-cards",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "   "},
        {"text": "hello", "exit_after": "false"},
        {"text": "hello", "unknown": "silently ignored before"},
    ],
)
def test_turn_request_fails_loudly_on_malformed_payload(payload):
    # Arrange
    malformed_payload = payload
    # Act
    try:
        TurnRequest.model_validate(malformed_payload)
        error = None
    except ValidationError as exc:
        error = exc
    # Assert
    assert isinstance(error, ValidationError)


def test_validation_errors_are_safe_for_json_error_responses():
    # Arrange
    payload = {"text": "   "}
    # Act
    try:
        TurnRequest.model_validate(payload)
        error = None
    except ValidationError as exc:
        error = exc

    encoded = json.dumps(
        error.errors(include_url=False, include_context=False) if error else []
    )

    # Assert
    assert "text" in encoded


def test_turn_request_accepts_marker_bound_visible_delivery():
    # Arrange
    payload = {
        "text": "steer now\n<!-- delivery:d-1 -->",
        "visible_delivery_id": "d-1",
        "dispatch_id": "d-1",
        "from_agent": "scitex-cards",
    }
    # Act
    request = TurnRequest.model_validate(payload)
    # Assert
    assert (request.visible_delivery_id, request.text, request.dispatch_id) == (
        "d-1",
        payload["text"],
        "d-1",
    )


def test_turn_request_without_visible_delivery_retains_legacy_shape():
    # Arrange
    payload = {"text": "ordinary turn"}
    # Act
    request = TurnRequest.model_validate(payload)
    # Assert
    assert request.visible_delivery_id is None


@pytest.mark.parametrize(
    "text",
    [
        "steer now",
        "steer now\n<!-- delivery:other -->",
        "steer now\n<!-- delivery:d-10 -->",
        "steer now\n<!-- delivery:d-1-->",
    ],
    ids=["absent", "different", "prefix-only", "malformed-marker"],
)
def test_turn_request_rejects_unbound_visible_delivery(text):
    # Arrange
    payload = {"text": text, "visible_delivery_id": "d-1"}
    # Act
    try:
        TurnRequest.model_validate(payload)
        error = None
    except ValidationError as exc:
        error = exc
    # Assert
    assert error is not None and "not bound to the submitted text" in str(error)


@pytest.mark.parametrize("identity", ["", 42, True, [], {}])
def test_turn_request_rejects_invalid_visible_identity(identity):
    # Arrange
    payload = {
        "text": f"steer now\n<!-- delivery:{identity} -->",
        "visible_delivery_id": identity,
    }
    # Act
    try:
        TurnRequest.model_validate(payload)
        error = None
    except ValidationError as exc:
        error = exc
    # Assert
    assert isinstance(error, ValidationError)


def test_marker_bound_turn_still_rejects_unknown_fields():
    # Arrange
    payload = {
        "text": "steer now\n<!-- delivery:d-1 -->",
        "visible_delivery_id": "d-1",
        "untrusted": "must not pass",
    }
    # Act
    try:
        TurnRequest.model_validate(payload)
        errors = []
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
    # Assert
    assert [(error["loc"], error["type"]) for error in errors] == [
        (("untrusted",), "extra_forbidden")
    ]
