"""The MCP fleet inventory accepts only the versioned host authority."""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest

from scitex_agent_container._inventory import FleetInventoryResponse
from scitex_agent_container._mcp import _fleet_inventory_client as client_mod
from scitex_agent_container._mcp._fleet_inventory_client import (
    FleetInventoryAuthorityError,
    request_fleet_inventory,
)
from scitex_agent_container._mcp._tools import _agent


class _Response:
    def __init__(self, body: dict, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._raw


def _valid_body() -> dict:
    return {
        "schema_version": "sac.fleet-inventory/v1",
        "inventory_kind": "fleet",
        "authority": {
            "kind": "sac-host-listener",
            "host": "compute-03",
            "sac_version": "0.28.1",
        },
        "semantics": {
            "agent_status_axis": "definition-and-process-liveness",
            "activity_axis": "not-reported-do-not-infer",
        },
        "agents": [{"name": "figrecipe", "status": "running"}],
        "hosts": [
            {
                "host": "compute-03",
                "status": "responded",
                "instrument": "local_registry",
                "detail": "",
                "elapsed_ms": 4,
                "agents": 1,
            }
        ],
    }


@contextmanager
def _in_sif_with_inventory(body: dict):
    saved_env = os.environ.get("APPTAINER_CONTAINER")
    saved_request = client_mod.request_fleet_inventory
    saved_local = _agent.invoke_cli_json
    local_calls = []
    os.environ["APPTAINER_CONTAINER"] = "/images/sac.sif"
    client_mod.request_fleet_inventory = lambda **kwargs: FleetInventoryResponse.model_validate(
        body
    )
    _agent.invoke_cli_json = lambda argv: local_calls.append(argv)
    try:
        yield local_calls
    finally:
        client_mod.request_fleet_inventory = saved_request
        _agent.invoke_cli_json = saved_local
        if saved_env is None:
            os.environ.pop("APPTAINER_CONTAINER", None)
        else:
            os.environ["APPTAINER_CONTAINER"] = saved_env


def test_request_fleet_inventory_accepts_host_authority_and_forwards_filters():
    # Arrange
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return _Response(_valid_body())

    # Act
    result = request_fleet_inventory(
        capability="plotting",
        machine="compute-03",
        base_url="http://listener:7878",
        bearer="token",
        opener=opener,
    )
    # Assert
    assert {
        "authority": result.authority.kind,
        "agent": result.agents[0].name,
        "urls": seen,
    } == {
        "authority": "sac-host-listener",
        "agent": "figrecipe",
        "urls": [
            "http://listener:7878/v1/fleet/inventory?capability=plotting&machine=compute-03"
        ],
    }


def test_request_fleet_inventory_rejects_unversioned_partial_payload():
    # Arrange
    def opener(request, timeout):
        return _Response({"agents": [{"name": "figrecipe", "status": "defined"}]})

    # Act
    call = lambda: request_fleet_inventory(  # noqa: E731
        base_url="http://listener:7878", opener=opener
    )
    # Assert
    with pytest.raises(FleetInventoryAuthorityError, match="missing or incompatible"):
        call()


def test_request_fleet_inventory_rejects_old_listener_route_without_local_fallback():
    # Arrange
    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            404,
            "not found",
            {},
            io.BytesIO(b'{"error":"not found"}'),
        )

    # Act
    call = lambda: request_fleet_inventory(  # noqa: E731
        base_url="http://old-listener:7878", opener=opener
    )
    # Assert
    with pytest.raises(FleetInventoryAuthorityError, match="HTTP 404"):
        call()


def test_in_sif_agent_list_uses_host_authority_and_preserves_cli_envelope():
    # Arrange
    with _in_sif_with_inventory(_valid_body()) as local_calls:
        # Act
        result = _agent.agent_list()
    # Assert
    assert {
        "exit_code": result["exit_code"],
        "authority": result["data"]["authority"]["kind"],
        "agent": result["data"]["agents"][0]["name"],
        "local_calls": local_calls,
    } == {
        "exit_code": 0,
        "authority": "sac-host-listener",
        "agent": "figrecipe",
        "local_calls": [],
    }


def test_in_sif_agent_list_does_not_fallback_when_authority_fails():
    # Arrange
    saved_env = os.environ.get("APPTAINER_CONTAINER")
    saved_request = client_mod.request_fleet_inventory
    saved_local = _agent.invoke_cli_json
    local_calls = []

    def unavailable(**kwargs):
        raise FleetInventoryAuthorityError("old host listener")

    os.environ["APPTAINER_CONTAINER"] = "/images/sac.sif"
    client_mod.request_fleet_inventory = unavailable
    _agent.invoke_cli_json = lambda argv: local_calls.append(argv)
    try:
        # Act
        call = _agent.agent_list
        # Assert
        with pytest.raises(FleetInventoryAuthorityError, match="old host listener"):
            call()
    finally:
        client_mod.request_fleet_inventory = saved_request
        _agent.invoke_cli_json = saved_local
        if saved_env is None:
            os.environ.pop("APPTAINER_CONTAINER", None)
        else:
            os.environ["APPTAINER_CONTAINER"] = saved_env
