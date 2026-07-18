"""The auth-heal detector is a SPECIFIC INSTANCE of this general shape.

PR #758 gave the login-expired detector a ``DetectionOutcome`` (auth_failed / ok /
unknown) and a ``Roster``. Those are correct and deployed, and nothing here
replaces them. What this suite pins is the RELATIONSHIP: a ``DetectionOutcome``
is exactly a fleet of AgentStates projected onto one signal,
``is_login_required``, and the roster rule is exactly "absence is a value".

Stating that in a test rather than a comment means the two cannot drift apart
quietly — which is the failure mode this whole card exists to end.

The real detector runs for real against real captured panes (the same fixtures
the auth-status matcher suite uses). No mocks.
"""

from __future__ import annotations

from scitex_agent_container._agentstate import AgentState, assess, states_from_detection
from scitex_agent_container._authheal._detect import detect_login_expired

# Byte-identical to the auth-heal suite's fixtures: a banner frozen directly
# above the prompt across both reads is a corroborated wedge.
STUCK = "● Login expired · Please run /login\n────────\n❯\n────────\n  ctx:1%\n"
OK = "  continuing the task now\n────────\n❯\n────────\n  ctx:1%\n"


def states_for(captures, roster=()):
    """Run the REAL detector, then project its outcome into AgentState rows."""
    return {
        state.agent: state
        for state in states_from_detection(
            detect_login_expired(captures), roster=roster, captures=captures
        )
    }


def test_a_wedged_agent_projects_to_is_login_required_true():
    # Arrange
    captures = {"alpha": (STUCK, STUCK)}
    # Act
    states = states_for(captures)
    # Assert
    assert states["alpha"].is_login_required is True


def test_a_healthy_agent_projects_to_is_login_required_false():
    # Arrange
    captures = {"alpha": (OK, OK)}
    # Act
    states = states_for(captures)
    # Assert
    assert states["alpha"].is_login_required is False


def test_an_unreadable_pane_projects_to_none_not_false():
    """The whole point: an unread pane is UNKNOWN, never a clean bill of health."""
    # Arrange
    captures = {"alpha": (None, None)}
    # Act
    states = states_for(captures)
    # Assert
    assert states["alpha"].is_login_required is None


def test_a_moving_banner_is_not_a_wedge():
    """An agent QUOTING the incident while working must never be flagged."""
    # Arrange
    captures = {"alpha": (STUCK, OK)}
    # Act
    states = states_for(captures)
    # Assert
    assert states["alpha"].is_login_required is False


def test_a_registered_agent_absent_from_the_reading_gets_a_row():
    """The enumeration is a READING of the fleet; the roster is the population."""
    # Arrange
    captures = {"alpha": (OK, OK)}
    # Act
    states = states_for(captures, roster=("alpha", "scitex-hub"))
    # Assert
    assert "scitex-hub" in states


def test_an_agent_absent_from_the_reading_has_every_signal_none():
    # Arrange
    captures = {"alpha": (OK, OK)}
    # Act
    states = states_for(captures, roster=("alpha", "scitex-hub"))
    # Assert
    assert set(states["scitex-hub"].signals().values()) == {None}


def test_an_agent_absent_from_the_reading_assesses_unknown():
    """Absence becomes a loud value rather than a silence that reads as fine."""
    # Arrange
    captures = {"alpha": (OK, OK)}
    # Act
    states = states_for(captures, roster=("alpha", "scitex-hub"))
    # Assert
    assert assess(states["scitex-hub"]).exit_code() == 2


def test_the_raw_panes_travel_with_the_projected_state():
    """The verdict without the pane it came from is not re-examinable."""
    # Arrange
    captures = {"alpha": (STUCK, STUCK)}
    # Act
    states = states_for(captures)
    # Assert
    assert states["alpha"].raw["pane_run1"] == STUCK


def test_the_projection_covers_every_agent_the_detector_classified():
    """Nothing handed to the detector may vanish on the way into this shape."""
    # Arrange
    captures = {"alpha": (STUCK, STUCK), "beta": (OK, OK), "gamma": (None, None)}
    # Act
    states = states_for(captures)
    # Assert
    assert set(states) == {"alpha", "beta", "gamma"}


def test_a_wedged_projection_is_not_yet_a_refutation_on_its_own():
    """An auth-heal reading fills ONE signal, so the others are honestly unread.

    This is the projection behaving correctly, not a gap: auth-heal never looked
    at tmux or /proc, so its rows assess UNKNOWN rather than claiming a verdict
    those signals would have to support. A partial reading must not masquerade as
    a complete one.
    """
    # Arrange
    captures = {"alpha": (STUCK, STUCK)}
    # Act
    states = states_for(captures)
    # Assert
    assert assess(states["alpha"]).verdict is None


def test_a_fully_observed_wedged_agent_is_refuted():
    """The CONTROL: with the other load-bearing signals read, the wedge refutes."""
    # Arrange
    state = AgentState(
        agent="alpha",
        is_tmux_live=True,
        is_process_alive=True,
        is_login_required=True,
    )
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is False


# EOF
