"""``resolve_node_host`` / ``is_local_node`` across TWO stores.

These lived in ``test_state_db_comms_nodes.py`` until the PostgreSQL port
(2026-08-28) and moved here because the resolver is now the only place in the
codebase that reads BOTH backends in one call:

  * ``instances`` — still SQLite, still addressed by ``db_path``.
  * ``comms_nodes`` — PostgreSQL, addressed by ``SCITEX_STORE_DSN``.

That split is exactly what a test can get wrong silently, so every test below
takes BOTH fixtures. ``db_path`` alone would leave the comms_nodes half
pointing at the unreachable guard DSN (a stray write raises, which is the
guard working); ``pg_schema`` alone would leave the instances half writing to
whichever state.db the environment resolves — i.e. a real one.

The precedence contract is what these protect, and it survives the migration
unchanged: a live ``instances`` row WINS, ``comms_nodes`` is the fallback, and
a tombstoned comms node resolves to nothing.

PA-306: no mocks; real SQLite under ``tmp_path``, real PostgreSQL schema.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    register_comms_node,
    resolve_node_host,
    unregister_comms_node,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


def _insert_instance(db_path: Path, *, name: str, host: str, port: int) -> None:
    """A live ``instances`` row — the branch that must WIN."""
    with state_db.open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (id, name, host, scope, a2a_port, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "inst-1",
                name,
                host,
                "global",
                port,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )


# ---------------------------------------------------------------------------
# resolve_node_host — fallback to comms_nodes after instances misses
# ---------------------------------------------------------------------------


def test_an_unknown_name_resolves_to_nothing(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — both stores empty
    target_db = db_path
    # Act
    info = resolve_node_host(name="ghost", db_path=target_db)
    # Assert
    assert info is None


def test_a_comms_node_resolves_when_no_instance_row_exists(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info == {"host": "mba", "a2a_port": 8642}


def test_a_live_instance_row_wins_over_the_comms_node(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — the two stores deliberately disagree
    _insert_instance(db_path, name="lead", host="instances-host", port=9000)
    register_comms_node(name="lead", host="comms-host", a2a_port=8642)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info["host"] == "instances-host"


def test_a_tombstoned_comms_node_resolves_to_nothing(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    unregister_comms_node(name="lead")
    # Assert — the routing FLIP: an unregistered name must stop being an
    # address, or the forwarder keeps POSTing at a dead port.
    assert resolve_node_host(name="lead", db_path=db_path) is None


# ---------------------------------------------------------------------------
# is_local_node — the federated graph identifies cross-host targets
# ---------------------------------------------------------------------------


def test_a_comms_node_on_this_host_is_local(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    local = is_local_node(name="lead", local_host="mba", db_path=db_path)
    # Assert
    assert local


def test_a_comms_node_on_another_host_is_not_local(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — this is the ADR-0014 bug fix: a cross-host target must be
    # forwarded, not served locally.
    register_comms_node(name="lead", host="mba", a2a_port=8642)
    # Act
    local = is_local_node(name="lead", local_host="spartan", db_path=db_path)
    # Assert
    assert not local


def test_an_unknown_name_is_treated_as_local(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — forwarding a never-seen name would synthesise an SSRF
    # target from a self-claimed string.
    target_db = db_path
    # Act
    local = is_local_node(name="ghost", local_host="mba", db_path=target_db)
    # Assert
    assert local
