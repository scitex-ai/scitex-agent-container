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


# ---------------------------------------------------------------------------
# The notice NAMES what it found (incident 2026-08-14, card
# sac-tmux-prefix-match-false-alive-20260814): tmux prefix matching let a
# SIBLING session pin the no-op branch, and a notice that does not say WHICH
# session it believed in cannot be caught lying.
# ---------------------------------------------------------------------------


def test_names_the_session_it_believed_in():
    # Arrange
    expected = "(tmux session tui-dotfiles)"
    # Act
    actual = render_already_running(
        "dotfiles", "ALIVE (process: session up)", session="tui-dotfiles"
    )
    # Assert
    assert expected in actual


def test_names_the_pane_pid_when_resolvable():
    # Arrange
    expected = "(tmux session tui-dotfiles, pane pid 12345)"
    # Act
    actual = render_already_running(
        "dotfiles",
        "ALIVE (process: session up)",
        session="tui-dotfiles",
        pane_pid=12345,
    )
    # Assert
    assert expected in actual


def test_omits_the_pid_clause_when_the_pid_is_unresolvable():
    # Arrange — an honest notice folds an unknown away rather than printing
    # a fabricated placeholder.
    forbidden = "pane pid"
    # Act
    actual = render_already_running(
        "dotfiles", "ALIVE (process: session up)", session="tui-dotfiles"
    )
    # Assert
    assert forbidden not in actual


def test_omits_the_found_clause_entirely_without_a_session(notice):
    # Arrange — the plain two-arg call keeps its original first line.
    forbidden = "tmux session"
    # Act
    actual = notice
    # Assert
    assert forbidden not in actual


def test_render_start_noop_notice_names_the_agents_tui_session():
    # Arrange — real config/verdict shapes (name attr; verdict.render()),
    # a session name that cannot be running here.
    from scitex_agent_container._lifecycle._start_noop_notice import (
        render_start_noop_notice,
    )

    class _Cfg:
        name = "zz-noop-notice-zz"

    class _Verdict:
        @staticmethod
        def render() -> str:
            return "ALIVE (delivery: 1 subscriber)"

    # Act
    actual = render_start_noop_notice(_Cfg(), _Verdict())
    # Assert — the session sac owns for this agent is named on the first line.
    assert "(tmux session tui-zz-noop-notice-zz)" in actual
