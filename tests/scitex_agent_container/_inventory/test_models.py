"""Pydantic fleet-inventory wire contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scitex_agent_container._inventory import FleetInventoryResponse


def _payload(status: str = "running") -> dict:
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
        "agents": [{"name": "figrecipe", "status": status}],
        "hosts": [],
    }


def test_inventory_model_preserves_additive_agent_fields():
    # Arrange
    payload = _payload()
    payload["agents"][0]["future_field"] = "kept"
    # Act
    result = FleetInventoryResponse.model_validate(payload).wire_dict()
    # Assert
    assert result["agents"][0]["future_field"] == "kept"


def test_inventory_model_rejects_undefined_lifecycle_status():
    # Arrange
    payload = _payload("idle")
    # Act
    call = lambda: FleetInventoryResponse.model_validate(payload)  # noqa: E731
    # Assert
    with pytest.raises(ValidationError):
        call()
