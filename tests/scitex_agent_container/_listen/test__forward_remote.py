"""Coverage for the cross-host leg of ``POST /agents/<name>/send``.

On 2026-10-27 the fleet measured the brokered send path accepting a
turn and never surfacing it (0/4 surfaced cross-host while direct ssh
to the same sidecar surfaced 1/1): the host forward resolved the
agent's port but posted it to the LOCAL loopback, so a foreign agent's
port became a local URL. On 2026-10-31 the same shape reproduced live
as a loud refusal — the local loopback held nothing on the foreign
port — which is only luck: had a local agent bound that port, the
turn would have completed in the WRONG session.

:func:`forward_to_remote_runner` closes the hole: when the live row
says the agent is on another host, push over the fixed ssh transport
to the peer's loopback sidecar (the E6-proven path) and proxy the
canonical receipt — or fail loud naming host and port. No mocks:
real ``subprocess`` through the ``subprocess_shim`` ``ssh`` binary;
the routing decision is a pure function of the row, so most tests
need no store at all. The one end-to-end routing test uses the
throwaway ``pg_schema`` store (skips where PostgreSQL is down, per
repo practice) to prove the lookup delegates to the push.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scitex_agent_container._listen import _forward

_PEER_HOST = "host-peer"
_LOCAL_HOST = "host-here"
_PEER_PORT = 19019
_EXCHANGE_ID = "xch_20261031T000000Z_remote_leg_abcdef"


@pytest.fixture
def _remote_row(pg_schema: str):
    """A remote live row in a throwaway ``instances`` store.

    Opt-in (only the end-to-end routing test takes it): ``pg_schema``
    wires a throwaway schema via ``SCITEX_STORE_DSN`` and drops it
    after, so the live fleet store is never touched. Where PostgreSQL
    is unreachable the taking test skips, per repo practice; every
    other test in this file needs no store at all.
    """
    from scitex_agent_container._state import state_store

    state_store.record_instance_start(
        name="alice", host=_PEER_HOST, a2a_port=_PEER_PORT
    )
    yield


def _accepted_receipt() -> str:
    return json.dumps(
        {
            "exchange_id": _EXCHANGE_ID,
            "status_code": {
                "kind": "http",
                "code": 202,
                "message": f"accepted; poll `/v1/exchanges/{_EXCHANGE_ID}`",
            },
        }
    )


def _row(host: str | None, port: int | str | None) -> dict:
    # Arrange helper, not a test: the shared-store fact the routing
    # decision reads.
    return {"name": "alice", "host": host, "a2a_port": port}


# --- Routing: pure function of the row, no store, no subprocess -------


def test_remote_row_routes_to_peer_host_and_port() -> None:
    """A live row on another host with a port is an address."""
    # Arrange
    # Act
    target = _forward.select_remote_target(
        _row(_PEER_HOST, _PEER_PORT), local_host=_LOCAL_HOST
    )
    # Assert
    assert target == (_PEER_HOST, _PEER_PORT)


def test_no_row_routes_nowhere() -> None:
    """Unknown agent: ``None`` so the caller keeps its 404/409 refusal."""
    # Arrange
    # Act
    target = _forward.select_remote_target(None, local_host=_LOCAL_HOST)
    # Assert
    assert target is None


def test_local_host_row_routes_nowhere() -> None:
    """An agent on THIS host is the local forward's business, not ours."""
    # Arrange
    # Act
    target = _forward.select_remote_target(
        _row(_LOCAL_HOST, _PEER_PORT), local_host=_LOCAL_HOST
    )
    # Assert
    assert target is None


def test_remote_row_without_port_routes_nowhere() -> None:
    """A live row with no port is not an address — refuse, don't guess."""
    # Arrange
    # Act
    target = _forward.select_remote_target(
        _row(_PEER_HOST, None), local_host=_LOCAL_HOST
    )
    # Assert
    assert target is None


# --- Push: real ssh shim, explicit host+port, no store -----------------


def test_remote_push_returns_202_receipt(subprocess_shim) -> None:
    """The peer sidecar's canonical acceptance comes back as HTTP 202."""
    # Arrange
    subprocess_shim.install("ssh", stdout=_accepted_receipt(), exit=0)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert resp.status_code == 202


def test_remote_push_returns_peer_exchange_id(subprocess_shim) -> None:
    """The receipt is the PEER's exchange identity, not one we invent."""
    # Arrange
    subprocess_shim.install("ssh", stdout=_accepted_receipt(), exit=0)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert json.loads(bytes(resp.body).decode("utf-8"))["exchange_id"] == _EXCHANGE_ID


def test_remote_push_dials_the_peer_host(subprocess_shim) -> None:
    """The ssh leg targets the row's host — never the local loopback.

    This is the regression test for the 2026-10-27/31 hole: resolving
    the port while dropping the host turned a foreign agent's port
    into a local URL.
    """
    # Arrange
    subprocess_shim.install("ssh", stdout=_accepted_receipt(), exit=0)
    # Act
    asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert _PEER_HOST in subprocess_shim.argv_for("ssh")


def test_remote_push_reports_the_peer_port(subprocess_shim) -> None:
    """The remote curl posts to the row's port on the peer loopback."""
    # Arrange
    subprocess_shim.install("ssh", stdout=_accepted_receipt(), exit=0)
    # Act
    asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert f"127.0.0.1:{_PEER_PORT}/v1/turn" in " ".join(
        subprocess_shim.argv_for("ssh")
    )


def test_ssh_failure_returns_502(subprocess_shim) -> None:
    """A dead ssh leg is a loud 502, never a silent acceptance."""
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="connect refused", exit=1)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert resp.status_code == 502


def test_ssh_failure_names_peer_host_and_port(subprocess_shim) -> None:
    """The 502 must say WHERE it failed so the operator checks the peer."""
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="connect refused", exit=1)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert _PEER_HOST in json.loads(bytes(resp.body).decode("utf-8")).get("error", "")


def test_non_json_reply_returns_502(subprocess_shim) -> None:
    """Banners or garbage from the ssh leg must not parse as delivery."""
    # Arrange
    subprocess_shim.install("ssh", stdout="not json at all\n", exit=0)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert resp.status_code == 502


def test_non_202_receipt_returns_502(subprocess_shim) -> None:
    """Only the canonical non-final 202 receipt counts as acceptance.

    A synchronous 200-with-text is a real reply on the local path's
    terms, but this leg's contract is the exchange receipt the caller
    polls — accepting anything else would reintroduce the false-ACK
    shape the 202 discipline exists to remove.
    """
    # Arrange
    subprocess_shim.install("ssh", stdout=json.dumps({"text": "hi back"}), exit=0)
    # Act
    resp = asyncio.run(
        _forward.push_to_remote_runner(
            "alice", "hi", peer_host=_PEER_HOST, port=_PEER_PORT, timeout=5.0
        )
    )
    # Assert
    assert resp.status_code == 502


# --- End to end: store row in, peer receipt out -------------------------


def test_routing_delegates_remote_row_to_push(_remote_row, subprocess_shim) -> None:
    """A remote live row reaches the peer and returns its receipt.

    Needs PostgreSQL (throwaway ``pg_schema``); skips where none is
    reachable. This is the wiring proof: lookup routes, push delivers.
    """
    # Arrange
    subprocess_shim.install("ssh", stdout=_accepted_receipt(), exit=0)
    # Act
    resp = asyncio.run(
        _forward.forward_to_remote_runner(
            "alice", "hi", local_host=_LOCAL_HOST, timeout=5.0
        )
    )
    # Assert
    assert resp.status_code == 202
