"""A withdrawn ``comms_nodes`` entry must not refuse the restart after it.

``unregister_comms_node`` withdraws an entry when an agent stops. Until the
fix these tests pin, ``register_comms_node``'s collision check compared the
incoming target against that DEAD record, so an agent that came back on a
different port raised :class:`CommsNodeConflictError` and — because every
production caller swallows it best-effort — vanished from the federated
graph permanently, surfacing only as ``unknown_target`` at the far end.

``spec.a2a.port: auto`` makes "a different port" the normal outcome of a
restart, so this was ordinary lifecycle rather than an edge case. Reproduced
independently on two hosts 2026-08-20: ``business`` live on 19012 behind a
tombstone pinned at 19033 (ywata-note-win, 19 live / 132 tombstoned), and
``scitex-dev`` live on 19008 behind an 11-day-old tombstone at 19003
(compute-04, 3 live / 29 tombstoned).

The revival assertions matter as much as the "does not raise" ones: making
the guard skip a dead record makes the re-point that follows it reachable
for the first time, so a registration could stop raising and still leave an
entry readers filter out.

ON POSTGRESQL SINCE 2026-08-28. The tombstone is now ``hide()`` rather than
an ``ended_at`` column, which is what makes it PROPAGATE — the old soft
tombstone existed to be shipped by ``export_state`` and was then dropped by
``import_state``'s INSERT OR IGNORE, so a withdrawal reached no peer at all.
The behaviour asserted here is unchanged; only the mechanism moved.

Separate file because ``test_state_db_comms_nodes.py`` is already near the
512-line cap. PA-306: no mocks; a real store via the ``pg_schema`` fixture.
"""

from __future__ import annotations

import socket

import pytest

from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    lookup_comms_node,
    register_comms_node,
    unregister_comms_node,
)


#: A source host that CANNOT be this one, derived rather than written down.
#:
#: It was the literal "scitex-compute-04" until 2026-08-28, and that made
#: this control silently self-defeating on exactly one runner: ``Store.node``
#: is ``socket.gethostname()``, so on the box actually NAMED
#: scitex-compute-04 the "other host" in the test IS this host, the origin
#: check compares equal, and the guard correctly declines to report a
#: cross-host conflict. The test then failed for a reason that had nothing
#: to do with the code under test — measured: py3.11 landed on that runner
#: and failed, py3.13 landed elsewhere and passed, same commit.
#:
#: A control whose value can COINCIDE with the thing it is controlling
#: against is not a control. Deriving it from this host's own name makes the
#: difference true by construction on every runner, forever.
FOREIGN_HOST = f"not-{socket.gethostname()}"


def _stopped_agent(*, port: int = 19033) -> None:
    """Register an agent, then stop it — leaving a withdrawn entry at ``port``."""
    register_comms_node(name="business", host="ywata-note-win", a2a_port=port)
    unregister_comms_node(name="business")


# ---------------------------------------------------------------------------
# The defect: a dead record blocked the restart that followed it
# ---------------------------------------------------------------------------


def test_restart_on_a_new_port_is_visible_to_peers_again(pg_schema: str) -> None:
    # Arrange — stopped at 19033, comes back on 19012 (port: auto moved it)
    _stopped_agent()
    # Act — raised CommsNodeConflictError before the fix
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19012)
    # Assert — lookup omits withdrawn entries, so a non-None answer proves
    # both that the call was accepted AND that the entry was un-hidden.
    assert lookup_comms_node(name="business") is not None


def test_the_revived_entry_carries_the_new_port(pg_schema: str) -> None:
    # Arrange
    _stopped_agent()
    # Act
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19012)
    # Assert — peers must route to where the agent actually listens
    row = lookup_comms_node(name="business")
    assert row is not None and int(row["a2a_port"]) == 19012


# ---------------------------------------------------------------------------
# Controls — the fix must not become "the guard is disabled"
# ---------------------------------------------------------------------------


def test_a_live_entry_on_a_different_port_still_refuses(pg_schema: str) -> None:
    # Arrange — LIVE registration, never unregistered
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19033)

    # Act
    def _reregister() -> None:
        register_comms_node(name="business", host="ywata-note-win", a2a_port=19012)

    # Assert — the collision this guard exists for must still fire
    with pytest.raises(CommsNodeConflictError):
        _reregister()


def test_a_cross_host_claim_over_a_withdrawn_entry_still_refuses(
    pg_schema: str,
) -> None:
    # Arrange — a withdrawn entry whose origin is THIS host
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19033)
    unregister_comms_node(name="business")

    # Act
    def _claim_from_another_host() -> None:
        register_comms_node(
            name="business",
            host=FOREIGN_HOST,
            a2a_port=19012,
            source_host=FOREIGN_HOST,
        )

    # Assert — another host claiming the name is an ADR-0014 uniqueness
    # question, which a tombstone does not answer.
    with pytest.raises(CommsNodeConflictError):
        _claim_from_another_host()


def test_an_unrelated_live_name_is_untouched_by_the_revival(pg_schema: str) -> None:
    # Arrange — a neighbour holding a port of its own
    _stopped_agent()
    register_comms_node(name="scitex-hpc", host="ywata-note-win", a2a_port=19012)
    # Act — business revives onto a different port than the neighbour's
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19099)
    # Assert — reviving one name must not disturb another's entry
    row = lookup_comms_node(name="scitex-hpc")
    assert row is not None and int(row["a2a_port"]) == 19012
