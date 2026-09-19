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
    request = TurnRequest.model_validate(
        payload
    )

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
