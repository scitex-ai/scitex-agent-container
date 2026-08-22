"""A live row with no port must not end the search for an ADDRESS.

`resolve_node_host` answers LOCALITY ("which host"), and `is_local_node` reads
only its `host`. The forwarder needed ADDRESSABILITY ("where do I POST") and was
reading the same value, so a live `instances` row with a NULL port was returned
as the answer, the forwarder took it, and `_forward_to_remote` 502'd on the
falsy port — never consulting `comms_nodes`, which may hold a working address
for that same name.

MEASURED on ywata-note-win 2026-08-20: `scitex-dev host=scitex-compute-04
a2a_port=NULL bound_port=NULL ended_at=NULL`. Live and PERMANENT — the GC never
reaps cross-host rows (`AND remote=0`, deliberate) and nothing back-fills the
port, so it can neither age out nor be repaired in place.

The controls are the weight-bearing half: locality must NOT move. A "fix" that
made `resolve_node_host` fall through would hand the locality decision to
`comms_nodes`, which may name a different host — silently redefining "local".

PA-306: no mocks; real on-disk SQLite under `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_forward import resolve_forward_target
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    register_comms_node,
    resolve_node_host,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


def _live_row(db_path: Path, *, a2a_port, bound_port, host: str = "host-a") -> None:
    """One live instances row for 'peer' with the given port columns."""
    with state_db.open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (id, name, host, scope, a2a_port, "
            " bound_port, started_at, ended_at) "
            "VALUES ('id-1', 'peer', ?, 'global', ?, ?, "
            "        '2026-08-20T00:00:00Z', NULL)",
            (host, a2a_port, bound_port),
        )


# ---------------------------------------------------------------------------
# The defect: a portless live row ended the search
# ---------------------------------------------------------------------------


def test_a_portless_live_row_falls_through_to_comms_nodes(db_path: Path) -> None:
    # Arrange — the exact fleet state: live row, both ports NULL, and a
    # comms_nodes entry that DOES carry an address.
    _live_row(db_path, a2a_port=None, bound_port=None)
    register_comms_node(
        name="peer", host="host-b", a2a_port=19099, db_path=db_path
    )
    # Act
    target = resolve_forward_target(name="peer", db_path=db_path)
    # Assert — resolved to {host-a, None} before, and 502'd downstream
    assert target is not None and target["a2a_port"] == 19099


def test_no_address_anywhere_returns_none(db_path: Path) -> None:
    # Arrange — live row with no port, and nothing in comms_nodes either
    _live_row(db_path, a2a_port=None, bound_port=None)
    # Act
    target = resolve_forward_target(name="peer", db_path=db_path)
    # Assert — "cannot forward", not a fabricated target
    assert target is None


# ---------------------------------------------------------------------------
# Controls — the instances row still WINS when it can answer
# ---------------------------------------------------------------------------


def test_a_usable_instances_row_wins_over_comms_nodes(db_path: Path) -> None:
    # Arrange — both sources have an address; instances is authoritative
    _live_row(db_path, a2a_port=19001, bound_port=None)
    register_comms_node(
        name="peer", host="host-b", a2a_port=19099, db_path=db_path
    )
    # Act
    target = resolve_forward_target(name="peer", db_path=db_path)
    # Assert — the fall-through must not become a preference for comms_nodes
    assert target is not None and target["a2a_port"] == 19001


def test_bound_port_counts_as_a_usable_address(db_path: Path) -> None:
    # Arrange — only bound_port survived on the row
    _live_row(db_path, a2a_port=None, bound_port=19012)
    register_comms_node(
        name="peer", host="host-b", a2a_port=19099, db_path=db_path
    )
    # Act
    target = resolve_forward_target(name="peer", db_path=db_path)
    # Assert — still the instances row, via the fallback column
    assert target is not None and target["a2a_port"] == 19012


# ---------------------------------------------------------------------------
# Controls — LOCALITY must be untouched by all of this
# ---------------------------------------------------------------------------


def test_locality_still_comes_from_the_instances_row(db_path: Path) -> None:
    # Arrange — portless local row, with comms_nodes naming a DIFFERENT host
    _live_row(db_path, a2a_port=None, bound_port=None, host="host-a")
    register_comms_node(
        name="peer", host="host-b", a2a_port=19099, db_path=db_path
    )
    # Act
    local = is_local_node(name="peer", local_host="host-a", db_path=db_path)
    # Assert — the agent IS on host-a; forwarding must not redefine that
    assert local is True


def test_resolve_node_host_still_reports_the_portless_row(db_path: Path) -> None:
    # Arrange
    _live_row(db_path, a2a_port=None, bound_port=None)
    # Act
    info = resolve_node_host(name="peer", db_path=db_path)
    # Assert — unchanged: it answers locality, port or no port
    assert info is not None and info["host"] == "host-a"
