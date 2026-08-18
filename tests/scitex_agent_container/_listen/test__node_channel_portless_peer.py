"""A live agent on another host must be reachable even when OUR row is portless.

MEASURED 2026-08-18, and it cost the fleet a night of coordination. Three
agents independently could not reach each other:

    scitex-dev  -> handyman-c03-01   404 "not in the host fleet registry"
    sac         -> scitex-hub        502 "missing a2a_port in instances row"
    scitex-hub  -> sac               nothing; concluded sac might be dead

ONE CAUSE. ``_listen/_node_channel.py`` forwards cross-host using
``target_info["a2a_port"]`` from ``resolve_node_host()``, which reads the
``instances`` / ``comms_nodes`` rows and returns whatever they carry — null
included. The only writer of a peer-visible port is
``_on_start_propagate.propagate_remote_start``, called from exactly ONE place:
``host_group.py``, the ``sac --on <peer> agents start`` path.

Every agent started the ordinary way — ``ssh <host> 'sac agents start <name>'``
— therefore propagates nothing, and is PORTLESS to every peer forever after.
Its sidecar is bound and healthy (measured: 19002-19010 all listening on
compute-03); the transport works; the DIRECTORY is empty, and the directory is
what routing reads.

WHY THE OBVIOUS FIX IS INSUFFICIENT, stated so nobody spends an afternoon on
it. ``_send_resolve.py`` already repairs this class for ``sac agents send`` by
falling back to the durable ``port_allocator`` claim. That claim is LOCAL: the
forwarding host holds no claim for an agent on another machine. Adding it here
would fix local split-brain and leave cross-host exactly as broken.

THE FIX IS PULL, NOT PUSH. When the local row carries no port, ASK the target
host rather than refusing — sac already has that shape in
``should_broker_peer_lookup`` + ``resolve_send_endpoint_via_host``, brokering to
the host's ``sac listen``, which is the same door ``agent_status`` uses.
Propagation-on-every-start was considered and rejected: it can only ever repair
agents started AFTER it ships, so everything running today would stay invisible
until restarted, and restarting the fleet to repair a directory is a worse cure
than the disease. scitex-dev reached the same conclusion independently.

WHAT THIS FILE DOES **NOT** CHANGE. ``test__node_channel_forwarders.py`` pins
the 502-when-portless contract, and that contract stays CORRECT for the case it
describes: nothing resolved anywhere. The fix must ADD a case (the host knows)
rather than flip those tests — a 502 is still the right answer when the target
host is asked and has no answer either. Flipping them would trade a false
refusal for a false acceptance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import record_lineage

TOKEN = "test-portless-peer-token"


@pytest.fixture
def portless_peer_env(tmp_path: Path):
    """A peer agent that is LIVE on another host but whose row has no port.

    This is the exact state every ``ssh <host> 'sac agents start'`` leaves
    behind on every other host in the fleet.
    """
    saved = {
        "HOME": os.environ.get("HOME"),
        "SCITEX_AGENT_CONTAINER_STATE_DB": os.environ.get(
            "SCITEX_AGENT_CONTAINER_STATE_DB"
        ),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
        ),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
        ),
    }
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_db_const = state_db.DEFAULT_DB_PATH

    db = tmp_path / "state.db"
    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)

    record_lineage(child="alice", parent="root", db_path=db)
    record_lineage(child="faraway", parent="root", db_path=db)

    # The defect, materialised: a LIVE remote agent with a NULL port. Written
    # the way a direct start leaves it, not the way `--on` would.
    state_db.record_instance_start(
        name="faraway",
        host="scitex-compute-03",
        a2a_port=None,
        bound_port=None,
        remote=True,
        db_path=db,
    )

    try:
        yield {"db": db}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _send_to_faraway(client: TestClient) -> object:
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "ping"}],
            },
            "metadata": {"from_agent": "alice"},
        },
    }
    return client.post(
        "/agents/faraway/message:send",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_portless_remote_row_is_the_state_a_direct_start_leaves(
    portless_peer_env,
) -> None:
    # Arrange: CHARACTERISATION. Pins the precondition the xfail below depends
    # on — if a future change starts populating the port at record time, this
    # goes red and tells the next reader the xfail's premise has moved.
    from scitex_agent_container._state.state_db_nodes import resolve_node_host

    # Act
    info = resolve_node_host(name="faraway", db_path=portless_peer_env["db"])
    # Assert
    assert info is not None and info.get("a2a_port") is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PULL-not-PUSH fix not yet implemented. Today the forwarder reads the "
        "local row's a2a_port and refuses with 502 when it is null, so every "
        "agent started via `ssh <host> 'sac agents start'` is unreachable "
        "cross-host despite a healthy, bound sidecar. The fix is to ask the "
        "TARGET HOST when the local row has no port — the same brokered "
        "lookup `agent_status` already uses. When that lands this test XPASSes "
        "and, being strict, fails until the marker is removed. Do NOT satisfy "
        "it by flipping the 502 contract in "
        "test__node_channel_forwarders.py: a 502 is still correct when the "
        "target host is asked and has no answer either."
    ),
)
def test_a_live_portless_peer_is_still_reachable(portless_peer_env) -> None:
    # Arrange: the agent is up on another host; only OUR directory is empty.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    # Act
    with TestClient(app) as client:
        resp = _send_to_faraway(client)
    # Assert: any outcome except "I refused because my own row was empty".
    assert "missing a2a_port" not in resp.text
