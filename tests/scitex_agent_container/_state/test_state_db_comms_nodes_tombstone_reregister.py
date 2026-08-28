"""A tombstoned ``comms_nodes`` record must not refuse the restart after it.

``unregister_comms_node`` tombstones a record when an agent stops. Until this
fix, ``register_comms_node``'s collision check compared the incoming target
against that DEAD record, so an agent that came back on a different port raised
:class:`CommsNodeConflictError` and — because every production caller swallows
it best-effort — vanished from the federated graph permanently, surfacing only
as ``unknown_target`` at the far end.

``spec.a2a.port: auto`` makes "a different port" the normal outcome of a
restart, so this was ordinary lifecycle rather than an edge case. Reproduced
independently on two hosts 2026-08-20: ``business`` live on 19012 behind a
tombstone pinned at 19033 (ywata-note-win, 19 live / 132 tombstoned), and
``scitex-dev`` live on 19008 behind an 11-day-old tombstone at 19003
(compute-04, 3 live / 29 tombstoned).

The revival assertions matter as much as the "does not raise" ones: fixing the
lookup makes the re-activation write below it reachable for the first time, so
a registration could stop raising and still leave a record readers filter out.

WHAT CHANGED WITH THE POSTGRESQL PORT (2026-08-28)
==================================================
The tombstone is no longer an ``ended_at`` column the code tests for
truthiness; it is ``Store.hide``, and the branch keys off ``row.hidden``. The
BEHAVIOUR asserted here is identical, which is the point of keeping the file
rather than rewriting the scenario: the same incident must stay covered across
the storage change. Isolation moved from a ``tmp_path`` SQLite file to the
``pg_schema`` throwaway schema — under one shared store there is no per-path
isolation to have.

PA-306: no mocks; the module is exercised through its real public surface.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    list_comms_nodes,
    lookup_comms_node,
    register_comms_node,
    unregister_comms_node,
)


def _stopped_agent(*, port: int = 19033) -> None:
    """Register an agent, then stop it — leaving a tombstone at ``port``."""
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
    # Assert — lookup filters tombstones, so a non-None record proves both
    # that the call was accepted AND that the record was un-hidden.
    assert lookup_comms_node(name="business") is not None


def test_the_revived_record_carries_the_new_port(pg_schema: str) -> None:
    # Arrange
    _stopped_agent()
    # Act
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19012)
    # Assert — peers must route to where the agent actually listens
    row = lookup_comms_node(name="business")
    assert row is not None and int(row["a2a_port"]) == 19012


def test_the_revived_record_is_not_a_second_record(pg_schema: str) -> None:
    # Arrange — the identity is the name, so a revival must reuse it
    # rather than mint a duplicate the listing would show twice.
    _stopped_agent()
    # Act
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19012)
    # Assert
    assert len(list_comms_nodes(include_ended=True)) == 1


# ---------------------------------------------------------------------------
# Controls — the fix must not become "the guard is disabled"
# ---------------------------------------------------------------------------


def test_a_live_record_on_a_different_port_still_refuses(pg_schema: str) -> None:
    # Arrange — LIVE registration, never unregistered
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19033)

    # Act
    def _reregister() -> None:
        register_comms_node(
            name="business", host="ywata-note-win", a2a_port=19012
        )

    # Assert — the collision this guard exists for must still fire
    with pytest.raises(CommsNodeConflictError):
        _reregister()


def test_a_cross_host_tombstone_still_refuses(pg_schema: str) -> None:
    # Arrange — tombstone owned by one source_host
    register_comms_node(
        name="business",
        host="ywata-note-win",
        a2a_port=19033,
        source_host="ywata-note-win",
    )
    unregister_comms_node(name="business")

    # Act
    def _claim_from_another_host() -> None:
        register_comms_node(
            name="business",
            host="scitex-compute-04",
            a2a_port=19012,
            source_host="scitex-compute-04",
        )

    # Assert — another host claiming the name is an ADR-0014 uniqueness
    # question, which a tombstone does not answer.
    with pytest.raises(CommsNodeConflictError):
        _claim_from_another_host()


def test_an_unrelated_live_name_is_untouched_by_the_revival(
    pg_schema: str,
) -> None:
    # Arrange — a neighbour holding a port of its own
    _stopped_agent()
    register_comms_node(name="scitex-hpc", host="ywata-note-win", a2a_port=19012)
    # Act — business revives onto a different port than the neighbour's
    register_comms_node(name="business", host="ywata-note-win", a2a_port=19099)
    # Assert — reviving one name must not disturb another's record
    row = lookup_comms_node(name="scitex-hpc")
    assert row is not None and int(row["a2a_port"]) == 19012
