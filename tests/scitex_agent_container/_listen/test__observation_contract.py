"""Strict public observation types refuse private fields and ambiguous numbers."""

import pytest
from pydantic import ValidationError

from scitex_agent_container._listen._observation_contract import (
    ObservationFrame,
    ObservationSource,
    RuntimeObservation,
)


@pytest.mark.parametrize("stamp", [True, "106", float("nan"), float("inf")])
def test_observation_frame_rejects_ambiguous_clock_values(stamp):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValidationError):
        ObservationFrame(
            producer_epoch="a" * 32, order=1, observed_at=stamp, clock="valid"
        )


def test_source_type_refuses_raw_path_payload():
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValidationError):
        ObservationSource(
            kind="owned-native-source/v1", identity="a" * 64, path="/private/source"
        )


@pytest.mark.parametrize("delta", [True, "0", float("nan"), float("inf")])
def test_delta_types_do_not_coerce_missing_or_invalid_evidence(delta):
    # Arrange — HEARTBEAT SPEC: the contract carries byte deltas, never
    # step counters. Ambiguous numbers must raise, not coerce.
    # Act
    # Assert
    with pytest.raises(ValidationError):
        RuntimeObservation(session_jsonl_delta_bytes=delta)


def test_delta_types_accept_measured_floats():
    # Arrange — measured deltas are floats; the contract accepts them.
    # Act
    observed = RuntimeObservation(
        session_jsonl_delta_bytes=28.0, subagent_jsonl_delta_bytes=0.0
    )
    # Assert
    assert (
        observed.session_jsonl_delta_bytes,
        observed.subagent_jsonl_delta_bytes,
    ) == (28.0, 0.0)
