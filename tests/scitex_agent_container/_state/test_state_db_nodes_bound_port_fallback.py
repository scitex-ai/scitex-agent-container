"""``resolve_node_host`` must not discard a usable port sitting in its own row.

The instances row carries BOTH ``a2a_port`` and ``bound_port`` — the writers
populate them together (``record_instance_start(a2a_port=b, bound_port=b)``).
``resolve_node_host`` selected only ``a2a_port``, so a row where just
``bound_port`` survived resolved to "no port" and 502'd at the forwarder, while
the sibling resolver ``_send_resolve`` — which has preferred ``bound_port`` over
the legacy column since it was introduced — would have reached the same agent
from the same row. Two resolvers, one row, one moment, two answers.

The controls matter more than the fix here: preferring bound_port must not
change WHICH HOST a name resolves to, and must not disturb the tombstone and
fallback semantics that ``is_local_node`` and the comms_nodes fall-through
depend on.

PA-306: no mocks; real on-disk SQLite under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    resolve_node_host,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


def _row(db_path: Path, *, a2a_port, bound_port) -> None:
    """Insert one live instances row with the given port columns.

    Written directly rather than through ``record_instance_start`` on
    purpose: that writer sets ``a2a_port`` and ``bound_port`` from ONE
    argument, so it cannot express the very state under test — a row
    where only ``bound_port`` survives.
    """
    with state_db.open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (id, name, host, scope, a2a_port, "
            " bound_port, started_at, ended_at) "
            "VALUES ('id-1', 'peer', 'host-a', 'global', ?, ?, "
            "        '2026-08-20T00:00:00Z', NULL)",
            (a2a_port, bound_port),
        )


# ---------------------------------------------------------------------------
# The defect: a usable bound_port was thrown away
# ---------------------------------------------------------------------------


def test_bound_port_is_used_when_a2a_port_is_null(db_path: Path) -> None:
    # Arrange — only bound_port survived on this row
    _row(db_path, a2a_port=None, bound_port=19012)
    # Act
    info = resolve_node_host(name="peer", db_path=db_path)
    # Assert — resolved to 'no port' before the fix, then 502'd downstream
    assert info is not None and info["a2a_port"] == 19012


# ---------------------------------------------------------------------------
# Controls — the preference must change the PORT and nothing else
# ---------------------------------------------------------------------------


def test_a2a_port_still_wins_when_both_are_set(db_path: Path) -> None:
    # Arrange — both populated and deliberately different
    _row(db_path, a2a_port=19001, bound_port=19999)
    # Act
    info = resolve_node_host(name="peer", db_path=db_path)
    # Assert — the legacy column keeps precedence; this is a fallback, not a swap
    assert info is not None and info["a2a_port"] == 19001


def test_a_row_with_neither_port_still_resolves_to_none_port(db_path: Path) -> None:
    # Arrange — the state that 502s; still reported, not invented
    _row(db_path, a2a_port=None, bound_port=None)
    # Act
    info = resolve_node_host(name="peer", db_path=db_path)
    # Assert
    assert info is not None and info["a2a_port"] is None


def test_the_host_is_unchanged_by_the_port_preference(db_path: Path) -> None:
    # Arrange
    _row(db_path, a2a_port=None, bound_port=19012)
    # Act
    info = resolve_node_host(name="peer", db_path=db_path)
    # Assert — locality must not move when only the port source changes
    assert info is not None and info["host"] == "host-a"


def test_locality_is_unchanged_for_a_portless_row(db_path: Path) -> None:
    # Arrange — the case deliberately NOT given a fall-through
    _row(db_path, a2a_port=None, bound_port=None)
    # Act
    local = is_local_node(name="peer", local_host="host-a", db_path=db_path)
    # Assert — a live row still means "the agent is on that host"
    assert local is True


def test_an_unknown_name_still_resolves_to_none(db_path: Path) -> None:
    # Arrange — no row at all
    unknown = "never-registered"
    # Act
    info = resolve_node_host(name=unknown, db_path=db_path)
    # Assert — the fall-through must still return None, not a fabricated host
    assert info is None
