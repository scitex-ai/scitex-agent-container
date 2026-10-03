"""Host listener route for the authoritative (not A2A-peer) inventory."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from scitex_agent_container._inventory import FleetInventoryResponse
from scitex_agent_container._listen import _fleet_inventory as inventory_mod
from scitex_agent_container._listen._fleet_inventory import _response_from_cli
from scitex_agent_container._listen.server import create_app
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import (
    FleetListing,
    HostReport,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_render import (
    hosts_payload,
)

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
    with (
        _inventory_result(_cli_success()),
        TestClient(create_app(token=TOKEN)) as client,
    ):
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
    with (
        _inventory_result(_cli_success()),
        TestClient(create_app(token=TOKEN)) as client,
    ):
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


def _serialized_cli_data(listing: FleetListing) -> dict:
    return json.loads(
        json.dumps({"agents": listing.agents, "hosts": hosts_payload(listing)})
    )


def test_cli_metadata_preserves_success_failed_and_filtered_host_reports():
    # Arrange
    listing = FleetListing(
        agents=[{"name": "scitex-app", "status": "running", "host": "compute-03"}],
        reports=[
            HostReport("compute-03", "responded", "local_registry", "", 2, 1),
            HostReport("compute-01", "unreachable", "ssh", "ssh refused", 5),
            HostReport("compute-02", "not_queried", "none", "--no-fanout"),
        ],
        resolutions=(("localhost", "compute-03"),),
        suppressed_reason="--no-fanout",
        peers_known=2,
    )
    data = _serialized_cli_data(listing)
    # Act
    wire = _response_from_cli(data).wire_dict()
    round_trip = FleetInventoryResponse.model_validate_json(json.dumps(wire))
    # Assert
    assert {
        "hosts": round_trip.wire_dict()["hosts"],
        "agents": round_trip.wire_dict()["agents"],
        "schema": round_trip.schema_version,
        "authority": round_trip.authority.kind,
        "activity": round_trip.semantics.activity_axis,
    } == {
        "hosts": data["hosts"]["reports"],
        "agents": data["agents"],
        "schema": "sac.fleet-inventory/v1",
        "authority": "sac-host-listener",
        "activity": "not-reported-do-not-infer",
    }


def test_empty_filtered_fleet_keeps_unknown_host_count():
    # Arrange
    listing = FleetListing(
        reports=[HostReport("compute-02", "timed_out", "ssh", "deadline", 8000)],
        resolutions=(("target", "compute-02"),),
    )
    data = _serialized_cli_data(listing)
    # Act
    wire = _response_from_cli(data).wire_dict()
    # Assert
    assert (wire["agents"], wire["hosts"]) == ([], data["hosts"]["reports"])


@pytest.mark.parametrize("reports", [None, {}, "not a report list"])
def test_cli_metadata_rejects_malformed_reports(reports):
    # Arrange
    data = {"agents": [], "hosts": {"reports": reports}}
    # Act
    # Assert
    with pytest.raises(ValidationError, match="hosts"):
        _response_from_cli(data)


def test_cli_metadata_without_reports_does_not_become_an_empty_fleet():
    # Arrange
    data = {"agents": [], "hosts": {"responded": 0, "total": 1}}
    # Act
    # Assert
    with pytest.raises(ValidationError, match="hosts"):
        _response_from_cli(data)


def test_cli_metadata_does_not_relax_host_status_validation():
    # Arrange
    listing = FleetListing(
        reports=[HostReport("compute-02", "idle", "ssh", "unsupported state")]
    )
    data = _serialized_cli_data(listing)
    # Act
    # Assert
    with pytest.raises(ValidationError, match="status"):
        _response_from_cli(data)
