"""``sac agents state`` — the fleet-level exit code keeps the same ordering.

Per agent, UNKNOWN outranks a refutation. The fleet summary must do the same, and
for the same reason: 0 is the strongest claim available, and a run that failed to
read part of the fleet must not spell the code that means "read it all, it is
healthy". That collapse is exactly what let a wedged agent sit for hours while
systemd recorded a successful tick on every pass that had failed to look at it.
"""

from __future__ import annotations

from scitex_agent_container._agentstate import AgentState, assess
from scitex_agent_container.cli_pkg._agents_state import _fleet_exit_code

HEALTHY = {
    "is_tmux_live": True,
    "is_process_alive": True,
    "is_login_required": False,
}


def healthy(agent="alpha", **overrides):
    values = dict(HEALTHY)
    values.update(overrides)
    return assess(AgentState(agent=agent, **values))


def test_an_all_healthy_fleet_exits_zero():
    # Arrange
    assessments = [healthy("alpha"), healthy("beta")]
    # Act
    code = _fleet_exit_code(assessments)
    # Assert
    assert code == 0


def test_a_fleet_with_a_refuted_agent_exits_one():
    # Arrange
    assessments = [healthy("alpha"), healthy("beta", is_login_required=True)]
    # Act
    code = _fleet_exit_code(assessments)
    # Assert
    assert code == 1


def test_a_fleet_with_an_unreadable_agent_exits_two():
    # Arrange
    assessments = [healthy("alpha"), assess(AgentState.unknown("beta", "no session"))]
    # Act
    code = _fleet_exit_code(assessments)
    # Assert
    assert code == 2


def test_unknown_outranks_a_refutation_at_the_fleet_level():
    """A fleet holding BOTH must report the weaker claim, not the louder one."""
    # Arrange
    assessments = [
        healthy("alpha", is_login_required=True),
        assess(AgentState.unknown("beta", "no session")),
    ]
    # Act
    code = _fleet_exit_code(assessments)
    # Assert
    assert code == 2


def test_assessing_nobody_exits_two_not_zero():
    """An empty enumeration is not a finding about everybody.

    "The list came back empty" is the single most common way "we observed
    nothing" gets recorded as "the fleet is fine" — it is how a blind tmux read
    inside a container reports a healthy host. The clean code must be EARNED by
    having actually read something.
    """
    # Arrange
    assessments = []
    # Act
    code = _fleet_exit_code(assessments)
    # Assert
    assert code == 2


# EOF
