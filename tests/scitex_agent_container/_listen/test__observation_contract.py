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


@pytest.mark.parametrize("count", [True, "0", -1, 1.5])
def test_counter_types_do_not_coerce_missing_or_invalid_evidence(count):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValidationError):
        RuntimeObservation(tools_started=count)
