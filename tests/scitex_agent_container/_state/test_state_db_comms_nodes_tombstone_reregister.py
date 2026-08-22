"""A tombstoned ``comms_nodes`` row must not refuse the restart after it.

``unregister_comms_node`` tombstones a row (``ended_at`` set) when an agent
stops. Until this fix, ``register_comms_node``'s collision check compared the
incoming target against that DEAD row, so an agent that came back on a
different port raised :class:`CommsNodeConflictError` and — because every
production caller swallows it best-effort — vanished from the federated graph
permanently, surfacing only as ``unknown_target`` at the far end.

``spec.a2a.port: auto`` makes "a different port" the normal outcome of a
restart, so this was ordinary lifecycle rather than an edge case. Reproduced
independently on two hosts 2026-08-20: ``business`` live on 19012 behind a
tombstone pinned at 19033 (ywata-note-win, 19 live / 132 tombstoned), and
``scitex-dev`` live on 19008 behind an 11-day-old tombstone at 19003
(compute-04, 3 live / 29 tombstoned).

The revival assertions matter as much as the "does not raise" ones: fixing the
SELECT makes the re-activation UPDATE three lines below it reachable for the
first time, so a registration could stop raising and still leave a row that
readers filter out.

Separate file because ``test_state_db_comms_nodes.py`` is already 625 lines
against a 512-line cap. PA-306: no mocks; real on-disk SQLite under
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    lookup_comms_node,
    register_comms_node,
    unregister_comms_node,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


def _stopped_agent(db_path: Path, *, port: int = 19033) -> None:
    """Register an agent, then stop it — leaving a tombstone at ``port``."""
    register_comms_node(
        name="business", host="ywata-note-win", a2a_port=port, db_path=db_path
    )
    unregister_comms_node(name="business", db_path=db_path)


# ---------------------------------------------------------------------------
# The defect: a dead row blocked the restart that followed it
# ---------------------------------------------------------------------------


def test_restart_on_a_new_port_is_visible_to_peers_again(db_path: Path) -> None:
    # Arrange — stopped at 19033, comes back on 19012 (port: auto moved it)
    _stopped_agent(db_path)
    # Act — raised CommsNodeConflictError before the fix
    register_comms_node(
        name="business", host="ywata-note-win", a2a_port=19012, db_path=db_path
    )
    # Assert — lookup filters tombstones, so a non-None row proves both that
    # the call was accepted AND that ended_at was cleared.
    assert lookup_comms_node(name="business", db_path=db_path) is not None


def test_the_revived_row_carries_the_new_port(db_path: Path) -> None:
    # Arrange
    _stopped_agent(db_path)
    # Act
    register_comms_node(
        name="business", host="ywata-note-win", a2a_port=19012, db_path=db_path
    )
    # Assert — peers must route to where the agent actually listens
    row = lookup_comms_node(name="business", db_path=db_path)
    assert row is not None and int(row["a2a_port"]) == 19012


# ---------------------------------------------------------------------------
# Controls — the fix must not become "the guard is disabled"
# ---------------------------------------------------------------------------


def test_a_live_row_on_a_different_port_still_refuses(db_path: Path) -> None:
    # Arrange — LIVE registration, never unregistered
    register_comms_node(
        name="business", host="ywata-note-win", a2a_port=19033, db_path=db_path
    )

    # Act
    def _reregister() -> None:
        register_comms_node(
            name="business", host="ywata-note-win", a2a_port=19012, db_path=db_path
        )

    # Assert — the collision this guard exists for must still fire
    with pytest.raises(CommsNodeConflictError):
        _reregister()


def test_a_cross_host_tombstone_still_refuses(db_path: Path) -> None:
    # Arrange — tombstone owned by one source_host
    register_comms_node(
        name="business",
        host="ywata-note-win",
        a2a_port=19033,
        source_host="ywata-note-win",
        db_path=db_path,
    )
    unregister_comms_node(name="business", db_path=db_path)

    # Act
    def _claim_from_another_host() -> None:
        register_comms_node(
            name="business",
            host="scitex-compute-04",
            a2a_port=19012,
            source_host="scitex-compute-04",
            db_path=db_path,
        )

    # Assert — another host claiming the name is an ADR-0014 uniqueness
    # question, which a tombstone does not answer.
    with pytest.raises(CommsNodeConflictError):
        _claim_from_another_host()


def test_an_unrelated_live_name_is_untouched_by_the_revival(db_path: Path) -> None:
    # Arrange — a neighbour holding a port of its own
    _stopped_agent(db_path)
    register_comms_node(
        name="scitex-hpc", host="ywata-note-win", a2a_port=19012, db_path=db_path
    )
    # Act — business revives onto a different port than the neighbour's
    register_comms_node(
        name="business", host="ywata-note-win", a2a_port=19099, db_path=db_path
    )
    # Assert — reviving one name must not disturb another's row
    row = lookup_comms_node(name="scitex-hpc", db_path=db_path)
    assert row is not None and int(row["a2a_port"]) == 19012
