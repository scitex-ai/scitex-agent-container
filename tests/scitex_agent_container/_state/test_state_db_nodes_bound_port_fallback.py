"""``a2a_port``/``bound_port`` — the split is GONE, and this file says how.

WHAT THIS FILE USED TO TEST
===========================
The instances row carried BOTH columns and the writers populated them
together, so a row where only ``bound_port`` survived resolved to "no port"
in ``resolve_node_host`` and 502'd at the forwarder, while ``_send_resolve``
— which preferred ``bound_port`` — reached the same agent from the same row.
Two resolvers, one row, one moment, two answers. These tests pinned a
FALLBACK that papered over that.

WHAT IT TESTS NOW, AND WHY THE CHANGE IS NOT A COVERAGE LOSS
============================================================
The 2026-08-28 move to the shared store FOLDED the two columns into one
field: ``COALESCE(a2a_port, bound_port)`` on the way in,
``instance_as_dict`` mirroring the value back out under both KEYS on the way
out. The state the fallback existed for — one column set, the other NULL —
is no longer expressible, so a test that constructed it would be testing a
shape the schema cannot produce.

Deleting the file would have been the wrong trade: the PROPERTY the fallback
protected is still load-bearing, and it is now stronger. So each test is
re-pointed at the property rather than at the mechanism:

* "bound_port is used when a2a_port is NULL"  →  the two keys always AGREE.
* "a2a_port wins when both are set"           →  they cannot differ.
* the locality controls                       →  unchanged, and still the
  weight-bearing half: the port must never decide WHICH HOST a name resolves
  to.

PA-306: no mocks. Records are written through the production ``Store.put``
with the production schema, so every test needs ``pg_schema``.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_instances import live_instance_for_name
from scitex_agent_container._state.state_db_instances_store import (
    ACTOR,
    instances_schema,
    run_with_reconnect,
    strip_unset,
)
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    resolve_node_host,
)


def _record(*, a2a_port) -> None:
    """One live instances record for 'peer' with the given port.

    ``None`` is STRIPPED rather than written, which is how the production
    writer records "no port". A field written as None is a stamp, and for the
    IMMUTABLE fields that stamp is permanent.
    """
    from scitex_dev.store import NEW_RECORD

    values = strip_unset(
        {
            "name": "peer",
            "a2a_port": a2a_port,
            "started_at": "2026-08-20T00:00:00Z",
        }
    )
    values.update({"id": "id-1", "host": "host-a", "remote": False})
    run_with_reconnect(
        lambda store: store.put(values, expected_revision=NEW_RECORD, actor=ACTOR)
    )


# ---------------------------------------------------------------------------
# The defect's root cause, removed rather than handled
# ---------------------------------------------------------------------------


def test_the_schema_declares_only_one_port_field() -> None:
    # Arrange — two columns holding one fact is how the two drift, and they
    # had. A fallback treats the drift; one field prevents it.
    fields = set(instances_schema().fields)
    # Act
    duplicated = {"a2a_port", "bound_port"} <= fields
    # Assert
    assert duplicated is False


def test_the_two_port_keys_always_agree(pg_schema: str) -> None:
    # Arrange — the successor to "bound_port is used when a2a_port is NULL".
    # Seven readers prefer ``bound_port`` and two prefer ``a2a_port``; they
    # can no longer be pointed at different values.
    _record(a2a_port=19012)
    # Act
    row = live_instance_for_name("peer")
    # Assert
    assert row["a2a_port"] == row["bound_port"] == 19012


def test_a_record_with_no_port_reports_none_under_both_keys(
    pg_schema: str,
) -> None:
    # Arrange — the state that 502s; still reported, never invented.
    _record(a2a_port=None)
    # Act
    row = live_instance_for_name("peer")
    # Assert
    assert row["a2a_port"] is None and row["bound_port"] is None


# ---------------------------------------------------------------------------
# Controls — locality must be untouched by any of this
# ---------------------------------------------------------------------------


def test_the_host_is_unchanged_by_the_port(pg_schema: str) -> None:
    # Arrange
    _record(a2a_port=19012)
    # Act
    info = resolve_node_host(name="peer")
    # Assert — locality must not move when only the port changes
    assert info is not None and info["host"] == "host-a"


def test_a_portless_record_still_resolves_to_a_host(pg_schema: str) -> None:
    # Arrange — the case deliberately NOT given a fall-through: a live record
    # means "the agent is on that host", port or no port.
    _record(a2a_port=None)
    # Act
    info = resolve_node_host(name="peer")
    # Assert
    assert info is not None and info["a2a_port"] is None


def test_locality_is_unchanged_for_a_portless_record(pg_schema: str) -> None:
    # Arrange
    _record(a2a_port=None)
    # Act
    local = is_local_node(name="peer", local_host="host-a")
    # Assert
    assert local is True


def test_an_unknown_name_still_resolves_to_none(pg_schema: str) -> None:
    # Arrange — no record at all, so the comms_nodes fall-through runs.
    unknown = "never-registered"
    # Act
    info = resolve_node_host(name=unknown)
    # Assert — the fall-through must still return None, not a fabricated host
    assert info is None
