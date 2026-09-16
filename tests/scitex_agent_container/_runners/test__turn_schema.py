from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from scitex_agent_container._runners._turn_schema import TurnRequest


def test_turn_request_preserves_valid_routing_identity():
    request = TurnRequest.model_validate(
        {
            "text": "steer now",
            "exit_after": False,
            "dispatch_id": "dispatch-1",
            "from_agent": "scitex-cards",
        }
    )

    assert request.dispatch_id == "dispatch-1"
    assert request.from_agent == "scitex-cards"


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
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(payload)


def test_validation_errors_are_safe_for_json_error_responses():
    with pytest.raises(ValidationError) as captured:
        TurnRequest.model_validate({"text": "   "})

    encoded = json.dumps(
        captured.value.errors(include_url=False, include_context=False)
    )

    assert "text" in encoded
