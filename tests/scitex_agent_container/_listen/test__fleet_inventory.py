"""Host listener route for the authoritative (not A2A-peer) inventory."""

from __future__ import annotations

from contextlib import contextmanager

from starlette.testclient import TestClient

from scitex_agent_container._listen import _fleet_inventory as inventory_mod
from scitex_agent_container._listen.server import create_app

TOKEN = "inventory-test-token"


@contextmanager
def _inventory_result(result):
    saved = inventory_mod._invoke_inventory
    inventory_mod._invoke_inventory = lambda argv: result
    try:
        yield
    finally:
        inventory_mod._invoke_inventory = saved


def _cli_success() -> dict:
    return {
        "exit_code": 0,
        "stderr": "",
        "stdout": "",
        "data": {
            "agents": [
                {"name": "scitex-app", "status": "running", "host": "compute-03"}
            ],
            "hosts": [
                {
                    "host": "compute-03",
                    "status": "responded",
                    "instrument": "local_registry",
                    "detail": "",
                    "elapsed_ms": 2,
                    "agents": 1,
                }
            ],
        },
    }


def test_fleet_inventory_route_marks_host_authority_and_schema():
    # Arrange
    with _inventory_result(_cli_success()), TestClient(
        create_app(token=TOKEN)
    ) as client:
        # Act
        response = client.get(
            "/v1/fleet/inventory", headers={"authorization": f"Bearer {TOKEN}"}
        )
    # Assert
    body = response.json()
    assert {
        "http_status": response.status_code,
        "schema": body["schema_version"],
        "kind": body["inventory_kind"],
        "authority": body["authority"]["kind"],
        "activity": body["semantics"]["activity_axis"],
        "agent": body["agents"][0]["name"],
    } == {
        "http_status": 200,
        "schema": "sac.fleet-inventory/v1",
        "kind": "fleet",
        "authority": "sac-host-listener",
        "activity": "not-reported-do-not-infer",
        "agent": "scitex-app",
    }


def test_fleet_inventory_route_fails_loud_on_malformed_cli_shape():
    # Arrange
    bad = {"exit_code": 0, "stderr": "", "stdout": "[]", "data": []}
    with _inventory_result(bad), TestClient(create_app(token=TOKEN)) as client:
        # Act
        response = client.get(
            "/v1/fleet/inventory", headers={"authorization": f"Bearer {TOKEN}"}
        )
    # Assert
    assert (response.status_code, "malformed" in response.json()["error"]) == (
        502,
        True,
    )


def test_fleet_inventory_route_remains_distinct_from_agents_peer_route():
    # Arrange
    with _inventory_result(_cli_success()), TestClient(
        create_app(token=TOKEN)
    ) as client:
        headers = {"authorization": f"Bearer {TOKEN}"}
        # Act
        inventory = client.get("/v1/fleet/inventory", headers=headers).json()
        peers = client.get("/agents", headers=headers).json()
    # Assert
    assert {
        "inventory_kind": inventory["inventory_kind"],
        "peer_has_inventory_kind": "inventory_kind" in peers,
        "peer_has_sources": "sources" in peers,
    } == {
        "inventory_kind": "fleet",
        "peer_has_inventory_kind": False,
        "peer_has_sources": True,
    }


def test_fleet_inventory_route_turns_cli_exception_into_loud_gateway_error():
    # Arrange
    saved = inventory_mod._invoke_inventory

    def broken(argv):
        raise RuntimeError("fleet collector exploded")

    inventory_mod._invoke_inventory = broken
    try:
        with TestClient(create_app(token=TOKEN)) as client:
            # Act
            response = client.get(
                "/v1/fleet/inventory",
                headers={"authorization": f"Bearer {TOKEN}"},
            )
    finally:
        inventory_mod._invoke_inventory = saved
    # Assert
    assert (response.status_code, response.json()["error"]) == (
        502,
        "authoritative fleet inventory command raised",
    )
