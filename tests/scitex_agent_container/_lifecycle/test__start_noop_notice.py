"""The already-running notice states what holds and names what would act."""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._start_noop_notice import (
    render_already_running,
)


@pytest.fixture
def notice() -> str:
    return render_already_running("dotfiles", "ALIVE (delivery: 1 subscriber)")


def test_names_the_agent(notice):
    # Arrange
    expected = "dotfiles is already running"
    # Act
    actual = notice
    # Assert
    assert expected in actual


def test_keeps_the_liveness_evidence(notice):
    # Arrange
    expected = "ALIVE (delivery: 1 subscriber)"
    # Act
    actual = notice
    # Assert
    assert expected in actual


def test_does_not_claim_the_agent_was_started(notice):
    # Arrange
    forbidden = "started"
    # Act
    actual = notice
    # Assert
    assert forbidden not in actual


def test_says_plainly_that_nothing_was_launched(notice):
    # Arrange
    expected = "nothing launched"
    # Act
    actual = notice
    # Assert
    assert expected in actual


def test_restart_hint_carries_the_y_flag_the_command_requires(notice):
    # Arrange: `sac agents restart` refuses without -y, so a hint lacking it
    # fails when pasted (both commands executed to confirm, 2026-07-22).
    expected = "sac agents restart dotfiles -y"
    # Act
    actual = notice
    # Assert
    assert expected in actual


def test_stop_hint_omits_y_because_a_named_target_does_not_need_it(notice):
    # Arrange: stop's -y gate covers only the fleet-wide selection flags, so
    # it is NOT symmetric with restart — verified by running both.
    forbidden = "sac agents stop dotfiles -y"
    # Act
    actual = notice
    # Assert
    assert forbidden not in actual


def test_offers_the_stop_then_start_sequence(notice):
    # Arrange
    expected = "sac agents stop dotfiles && sac agents start dotfiles"
    # Act
    actual = notice
    # Assert
    assert expected in actual


def test_offers_the_force_escape_hatch(notice):
    # Arrange
    expected = "sac agents start dotfiles --force"
    # Act
    actual = notice
    # Assert
    assert expected in actual
