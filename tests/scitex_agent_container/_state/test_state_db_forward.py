"""A live record with no port must not end the search for an ADDRESS.

`resolve_node_host` answers LOCALITY ("which host"), and `is_local_node` reads
only its `host`. The forwarder needed ADDRESSABILITY ("where do I POST") and was
reading the same value, so a live `instances` record with a NULL port was
returned as the answer, the forwarder took it, and `_forward_to_remote` 502'd on
the falsy port — never consulting `comms_nodes`, which may hold a working
address for that same name.

MEASURED on ywata-note-win 2026-08-20: `scitex-dev host=scitex-compute-04
a2a_port=NULL bound_port=NULL ended_at=NULL`. Live and PERMANENT — the GC never
reaps cross-host rows (deliberate) and nothing back-fills the port, so it can
neither age out nor be repaired in place.

The controls are the weight-bearing half: locality must NOT move. A "fix" that
made `resolve_node_host` fall through would hand the locality decision to
`comms_nodes`, which may name a different host — silently redefining "local".

PA-306: no mocks. ONE store now, not two: ``comms_nodes`` moved to PostgreSQL
on 2026-08-28 and ``instances`` moved the same day, so ``db_path`` is gone from
both resolvers and every test here takes ``pg_schema`` alone.

ONE CONTROL CHANGED MEANING, AND IT IS WORTH SAYING WHY. ``bound_port`` is no
longer a second column that can survive alone: the store keeps ONE port field
and the codec mirrors it out under both keys. So "only bound_port survived" is
no longer a state the data can be in — which is the defect's root cause
removed, not merely its symptom. The test that asserted the fallback now
asserts the mirror instead, because that is the property that replaced it.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_forward import resolve_forward_target
from scitex_agent_container._state.state_db_instances_store import (
    ACTOR,
    run_with_reconnect,
    strip_unset,
)
from scitex_agent_container._state.state_db_nodes import (
    is_local_node,
    register_comms_node,
    resolve_node_host,
)


def _live_record(*, a2a_port, host: str = "host-a") -> None:
    """One live instances record for 'peer' with the given port.

    Written through the production ``Store.put`` with the production schema.
    ``None`` is STRIPPED rather than written, which is how the real writer
    records "no port": a field written as None is a stamp, and for the
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
    values.update({"id": "id-1", "host": host, "remote": False})
    run_with_reconnect(
        lambda store: store.put(values, expected_revision=NEW_RECORD, actor=ACTOR)
    )


# ---------------------------------------------------------------------------
# The defect: a portless live record ended the search
# ---------------------------------------------------------------------------


def test_a_portless_live_record_falls_through_to_comms_nodes(
    pg_schema: str,
) -> None:
    # Arrange — the exact fleet state: live record, no port, and a
    # comms_nodes entry that DOES carry an address.
    _live_record(a2a_port=None)
    register_comms_node(name="peer", host="host-b", a2a_port=19099)
    # Act
    target = resolve_forward_target(name="peer")
    # Assert — resolved to {host-a, None} before, and 502'd downstream
    assert target is not None and target["a2a_port"] == 19099


def test_no_address_anywhere_returns_none(pg_schema: str) -> None:
    # Arrange — live record with no port, and nothing in comms_nodes either
    _live_record(a2a_port=None)
    # Act
    target = resolve_forward_target(name="peer")
    # Assert — "cannot forward", not a fabricated target
    assert target is None


# ---------------------------------------------------------------------------
# Controls — the instances record still WINS when it can answer
# ---------------------------------------------------------------------------


def test_a_usable_instances_record_wins_over_comms_nodes(pg_schema: str) -> None:
    # Arrange — both sources have an address; instances is authoritative
    _live_record(a2a_port=19001)
    register_comms_node(name="peer", host="host-b", a2a_port=19099)
    # Act
    target = resolve_forward_target(name="peer")
    # Assert — the fall-through must not become a preference for comms_nodes
    assert target is not None and target["a2a_port"] == 19001


def test_the_bound_port_key_mirrors_the_one_stored_port(pg_schema: str) -> None:
    # Arrange — the successor to "only bound_port survived". Two columns
    # holding one fact is what let this resolver and `_send_resolve` give
    # different answers about the same row; the store keeps one field, so the
    # asymmetry has nowhere left to live. Seven readers still read the
    # ``bound_port`` KEY, and it must agree.
    _live_record(a2a_port=19012)
    from scitex_agent_container._state.state_db_instances import (
        live_instance_for_name,
    )

    row = live_instance_for_name("peer")
    # Act
    target = resolve_forward_target(name="peer")
    # Assert
    assert target["a2a_port"] == row["bound_port"] == 19012


# ---------------------------------------------------------------------------
# Controls — LOCALITY must be untouched by all of this
# ---------------------------------------------------------------------------


def test_locality_still_comes_from_the_instances_record(pg_schema: str) -> None:
    # Arrange — portless local record, with comms_nodes naming a DIFFERENT host
    _live_record(a2a_port=None, host="host-a")
    register_comms_node(name="peer", host="host-b", a2a_port=19099)
    # Act
    local = is_local_node(name="peer", local_host="host-a")
    # Assert — the agent IS on host-a; forwarding must not redefine that
    assert local is True


def test_resolve_node_host_still_reports_the_portless_record(
    pg_schema: str,
) -> None:
    # Arrange
    _live_record(a2a_port=None)
    # Act
    info = resolve_node_host(name="peer")
    # Assert — unchanged: it answers locality, port or no port
    assert info is not None and info["host"] == "host-a"
