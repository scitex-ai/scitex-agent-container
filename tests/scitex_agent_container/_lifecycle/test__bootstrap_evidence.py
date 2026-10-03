"""Selected source/timing authority and exact native mission boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._bootstrap_evidence import (
    admitted_native,
    mission_copy_count,
    spec_snapshot,
)
from scitex_agent_container.runtimes._codex_startup_admission import (
    StartupAdmissionProbe,
)

from . import test__bootstrap_complete as completion_source

completion = completion_source.completion


@pytest.mark.parametrize("count", [1, 2])
def test_only_exact_whole_source_copies_are_counted(count):
    # Arrange
    source = "one complete mandate\nwith boundaries"
    # Act
    measured = mission_copy_count(source * count, source)
    # Assert
    assert measured == count


@pytest.mark.parametrize(
    "body", ["", "mandate suffix", "prefix mandate", "mandate" * 3]
)
def test_partial_extra_or_three_copy_missions_are_refused(body):
    # Arrange
    source = "mandate"
    # Act
    # Assert
    with pytest.raises(RuntimeError):
        mission_copy_count(body, source)


def test_changed_selected_account_refuses_source_binding(completion):
    # Arrange
    case = completion
    case["config"].subscription_account = "different-account"
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="subscription_account"):
        spec_snapshot(case["config"], case["sha"])


def test_rehashed_different_formal_source_still_fails_the_original_pin(completion):
    # Arrange
    case = completion
    path = Path(case["config"].config_path)
    path.write_text(path.read_text() + "\n# changed authority\n")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="specification changed"):
        spec_snapshot(case["config"], case["sha"])


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), 101])
def test_nonfinite_or_future_admission_clock_cannot_be_observed(completion, timestamp):
    # Arrange
    case = completion
    probe = replace(case["probe"], issued_at=timestamp)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="timing"):
        admitted_native(case["config"], case["runtime"], probe, observed_at=100)


def test_probe_subclass_truth_override_is_rejected_without_callback(completion):
    # Arrange
    case = completion

    class ForgedProbe(StartupAdmissionProbe):
        def observed(self, **_kwargs):
            raise AssertionError("a virtual proof callback ran")

    probe = ForgedProbe(
        case["probe"].record,
        case["probe"].mission,
        case["probe"].issued_at,
        case["probe"].proc_root,
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="genuine native admission probe"):
        admitted_native(case["config"], case["runtime"], probe, observed_at=100)
