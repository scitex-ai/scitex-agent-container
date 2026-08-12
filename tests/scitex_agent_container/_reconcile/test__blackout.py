"""N corpses at once is ONE event, and it must not become N restarts.

The hole: `_tmux_probe` treats "no server running" as a CONFIRMED-empty fleet
(`{}`, a real observation), and `_verdict_tmux`'s blindness rescue fires only
inside a container. Under `systemd --user` on the host that empty reading
passes straight through as "every session is genuinely absent" — which is
exactly what a dead tmux server looks like, and exactly what happened on
2026-08-11 when the host's server took eleven agents with it in two seconds.

Ninety specs on that fleet declare a managed restart policy, and the budget
throttles per agent and per pass but never fleet-wide: 10/pass x 12 passes/hour
is up to 120 container starts an hour into a host that just lost its tmux
server — which `sac agents start` would happily recreate, so it does not even
fail fast.

These pin the predicate. Pure function, hand-built inputs, no tmux and no
daemon.
"""

from __future__ import annotations

from scitex_agent_container._reconcile._blackout import (
    FLEET_BLACKOUT_MIN_RESTARTS,
    blackout_detail,
    is_fleet_blackout,
)


# --- the incident this exists for ----------------------------------------


def test_many_corpses_and_no_sessions_anywhere_is_a_blackout():
    """2026-08-11, in one assertion: eleven corpses, zero live sessions."""
    # Arrange
    server, restarts = False, 11
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is True


def test_two_corpses_is_already_a_blackout():
    """The threshold is where the evidence changes, not at some round number."""
    # Arrange
    server, restarts = False, FLEET_BLACKOUT_MIN_RESTARTS
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is True


# --- and the cases it must NOT break -------------------------------------


def test_a_single_corpse_is_still_restarted():
    """On a one-agent host every real death also has zero live sessions.

    Refusing here would make the job useless for the case it handles best,
    and one restart is a blast radius the per-agent budget already bounds.
    """
    # Arrange
    server, restarts = False, 1
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is False


def test_a_live_server_with_every_agent_dead_is_still_recovered():
    """THE load-bearing negative, and the case an earlier draft got wrong.

    A tmux server that is up but holds no sessions means the AGENTS died, not
    the host — the 2026-06 OAuth rotation killed 33 of them with tmux
    untouched, and recovering exactly that is why this job exists. Keying the
    breaker on "zero sessions" would have blocked it; `test__pass.py::
    test_whole_dead_fleet_is_recovered` caught that on the first run.
    """
    # Arrange
    server, restarts = True, 33
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is False


def test_a_host_with_nothing_to_restart_is_not_a_blackout():
    """No server AND nothing to restart: a quiet host, not an incident."""
    # Arrange
    server, restarts = False, 0
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is False


def test_an_unobservable_fleet_never_trips_the_breaker():
    """``None`` is "I could not look" — it must not manufacture a blackout.

    An unobservable fleet is already handled upstream (every agent decides
    UNKNOWN and nothing is restarted); inventing a blackout from a reading
    nobody took would be the same manufactured certainty one layer along.
    """
    # Arrange
    server, restarts = None, 11
    # Act
    blackout = is_fleet_blackout(server_present=server, restart_count=restarts)
    # Assert
    assert blackout is False


# --- the operator-facing explanation --------------------------------------


def test_detail_names_the_withheld_agents():
    # Arrange
    names = ("figrecipe", "scitex-ui")
    # Act
    detail = blackout_detail(2, names)
    # Assert
    assert "figrecipe" in detail


def test_detail_says_it_refused_rather_than_failed():
    """A refusal read as a failure sends the operator hunting the wrong thing."""
    # Arrange
    names = ("a",)
    # Act
    detail = blackout_detail(11, names)
    # Assert
    assert "REFUSING" in detail


def test_detail_points_at_the_tmux_server():
    """The reader needs the next action, not just the verdict."""
    # Arrange
    names = ("a",)
    # Act
    detail = blackout_detail(11, names)
    # Assert
    assert "tmux server" in detail


def test_detail_truncates_a_long_withheld_list():
    # Arrange
    names = tuple(f"agent-{i}" for i in range(14))
    # Act
    detail = blackout_detail(14, names)
    # Assert
    assert "+4 more" in detail
