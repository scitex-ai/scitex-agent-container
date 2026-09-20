"""Typed wire contract for authoritative fleet inventory.

``agent_list`` and ``a2a_peers`` answer different questions.  The former is
the fleet's spec/runtime inventory and must come from the bare-host SAC
authority; the latter is the communication graph observed by one listener.
Keeping the inventory envelope versioned makes an old embedded SAC fail loud
instead of silently interpreting a partial local view as the fleet.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FLEET_INVENTORY_SCHEMA = "sac.fleet-inventory/v1"


class FleetInventoryAgent(BaseModel):
    """One CLI inventory row; preserve additive fields from newer SACs."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    status: Literal[
        "defined", "running", "auth-failed", "stopped", "invalid", "unknown"
    ]


class FleetInventoryHost(BaseModel):
    """Observation state for one host included in the fleet query."""

    model_config = ConfigDict(extra="allow")

    host: str = Field(min_length=1)
    status: Literal[
        "responded",
        "timed_out",
        "unreachable",
        "sac_missing",
        "sac_too_old",
        "malformed",
        "not_queried",
    ]
    instrument: str = Field(min_length=1)
    detail: str
    elapsed_ms: int = Field(ge=0)
    agents: int | None = Field(default=None, ge=0)


class FleetInventoryAuthority(BaseModel):
    """Provenance proving which process produced the inventory."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sac-host-listener"]
    host: str = Field(min_length=1)
    sac_version: str = Field(min_length=1)


class FleetInventorySemantics(BaseModel):
    """Declare the two state axes consumers have historically conflated."""

    model_config = ConfigDict(extra="forbid")

    agent_status_axis: Literal["definition-and-process-liveness"]
    activity_axis: Literal["not-reported-do-not-infer"]


class FleetInventoryResponse(BaseModel):
    """Versioned response returned by ``GET /v1/fleet/inventory``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["sac.fleet-inventory/v1"]
    inventory_kind: Literal["fleet"]
    authority: FleetInventoryAuthority
    semantics: FleetInventorySemantics
    agents: list[FleetInventoryAgent]
    hosts: list[FleetInventoryHost]

    def wire_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


__all__ = [
    "FLEET_INVENTORY_SCHEMA",
    "FleetInventoryAgent",
    "FleetInventoryAuthority",
    "FleetInventoryHost",
    "FleetInventoryResponse",
    "FleetInventorySemantics",
]
