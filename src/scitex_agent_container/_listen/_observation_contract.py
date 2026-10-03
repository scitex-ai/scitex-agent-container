"""Typed passive fleet evidence; all event and observation clocks are seconds."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ObservationSource(_Contract):
    kind: Literal["owned-native-source/v1"]
    identity: str = Field(pattern=r"^[a-f0-9]{64}$")


class ObservationAuthority(_Contract):
    agent: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    host: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    instance_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    boot_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    source: ObservationSource


class ObservationFrame(_Contract):
    producer_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    order: int = Field(ge=0)
    observed_at: FiniteFloat | None
    clock: Literal["valid", "uncertain"]


class LedgerPage(_Contract):
    limit: int = Field(gt=0)
    returned: int = Field(ge=0)
    complete: bool


class ExchangeObservation(_Contract):
    exchange_id: str
    authority: ObservationAuthority
    phase: Literal["pending", "expired", "not_proven", "proven", "unknown"]
    proven: bool | None
    issued_at: FiniteFloat | None
    deadline: FiniteFloat | None
    tool_completed_at: FiniteFloat | None
    verified_at: FiniteFloat | None
    observed_at: FiniteFloat | None
    handshake_lease_s: FiniteFloat | None
    issued_age_s: FiniteFloat | None
    tool_completed_age_s: FiniteFloat | None
    verified_age_s: FiniteFloat | None
    lease_remaining_s: FiniteFloat | None


class HandshakeSnapshot(_Contract):
    state: Literal["observed", "unknown"]
    reason: Literal[
        "",
        "ledger_unavailable",
        "history_incomplete",
        "ambiguous_order",
        "authority_unknown",
        "capability_unknown",
        "clock_uncertain",
    ]
    page: LedgerPage
    current_exchange: ExchangeObservation | None = None
    last_verified_reply: ExchangeObservation | None = None


class RuntimeObservation(_Contract):
    state: Literal["observed", "unknown"] = "unknown"
    authority: ObservationAuthority | None = None
    observed_at: FiniteFloat | None = None
    age_s: FiniteFloat | None = None
    heartbeat_lease_s: FiniteFloat | None = None
    lease_expires_at: FiniteFloat | None = None
    lease_remaining_s: FiniteFloat | None = None
    lease_expired: bool | None = None
    resident_state: Literal["idle", "active", "blocked"] | None = None
    progress_at: FiniteFloat | None = None
    progress_age_s: FiniteFloat | None = None
    progress_seq: int | None = Field(default=None, ge=0)
    progress_stale_s: FiniteFloat | None = None
    progress_is_stale: bool | None = None
    turns_accepted: int | None = Field(default=None, ge=0)
    turns_completed: int | None = Field(default=None, ge=0)
    tools_started: int | None = Field(default=None, ge=0)
    tools_completed: int | None = Field(default=None, ge=0)
    tools_inflight: int | None = Field(default=None, ge=0)


class AgentObservation(_Contract):
    schema_version: Literal["sac.agent-observation/v1"] = "sac.agent-observation/v1"
    authority: ObservationAuthority | None
    frame: ObservationFrame
    handshake: HandshakeSnapshot
    runtime: RuntimeObservation
