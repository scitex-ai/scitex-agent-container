"""Clock measurements stay unknown when absent, invalid, or ahead of observation."""

import math
from types import SimpleNamespace

import pytest

from scitex_agent_container._listen._handshake_observation import (
    handshake_observation,
    require_observation_time,
    server_verification_time,
)
from scitex_agent_container.runtimes._codex_activity import CodexActivityError


@pytest.fixture
def receipt():
    return {
        "contract": {"issued_at": 100.0, "deadline": 110.0},
        "status": {
            "proven": True,
            "tool_proof": {"completed_at": 102.0},
            "verified_at": 104.0,
        },
    }


@pytest.mark.parametrize(
    "value", [None, True, "104", math.nan, math.inf, 10**400, -1, 0, 107]
)
def test_absent_invalid_or_future_verification_age_is_unknown(receipt, value):
    # Arrange
    receipt["status"]["verified_at"] = value
    # Act
    result = handshake_observation(**receipt, observed_at=106)
    # Assert
    assert (result["verified_at"], result["verified_age_s"]) == (None, None)


@pytest.mark.parametrize("value", [None, True, math.nan, math.inf, 99, 107])
def test_unknown_or_impossible_completion_cannot_support_server_verification(
    receipt, value
):
    # Arrange
    receipt["status"]["tool_proof"]["completed_at"] = value
    # Act
    result = handshake_observation(**receipt, observed_at=106)
    # Assert
    assert (
        result["tool_completed_at"],
        result["tool_completed_age_s"],
        result["verified_at"],
    ) == (None, None, None)


def test_server_timestamp_before_native_completion_is_unknown(receipt):
    # Arrange
    receipt["status"]["verified_at"] = 101
    # Act
    result = handshake_observation(**receipt, observed_at=106)
    # Assert
    assert result["verified_age_s"] is None


def test_pending_transport_cannot_claim_successful_server_timestamp(receipt):
    # Arrange
    receipt["status"]["proven"] = None
    # Act
    result = handshake_observation(**receipt, observed_at=106)
    # Assert
    assert result["verified_at"] is None


@pytest.mark.parametrize("now", [None, True, math.nan, math.inf, 0, 99])
def test_invalid_regressed_server_clock_cannot_make_current_proof_decisions(
    receipt, now
):
    # Arrange
    contract = receipt["contract"]
    # Act
    # Assert
    with pytest.raises(CodexActivityError):
        require_observation_time(contract, now)


def test_expiry_keeps_real_historical_clocks_and_signed_remaining_lease(receipt):
    # Arrange
    # Act
    result = handshake_observation(**receipt, observed_at=112)
    # Assert
    assert (
        result["handshake_lease_s"],
        result["verified_age_s"],
        result["lease_remaining_s"],
    ) == (10, 8, -2)


def test_verification_uses_server_observation_not_tool_completion(receipt):
    # Arrange
    proof = SimpleNamespace(completed_at=102)
    # Act
    verified = server_verification_time(receipt["contract"], proof, observed_at=106)
    # Assert
    assert verified == 106
