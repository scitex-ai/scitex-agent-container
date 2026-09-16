"""Validated, evidence-bearing operational state for one agent."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class DefinitionState(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"


class ProcessState(str, Enum):
    ABSENT = "absent"
    STARTING = "starting"
    ALIVE = "alive"
    EXITED = "exited"
    UNKNOWN = "unknown"


class TurnState(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    PREFILLING = "prefilling"
    REASONING = "reasoning"
    TOOL_RUNNING = "tool_running"
    DECODING = "decoding"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class CommunicationState(str, Enum):
    REACHABLE = "reachable"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class ProgressState(str, Enum):
    ADVANCING = "advancing"
    STALE = "stale"
    UNKNOWN = "unknown"


class ObservationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    detail: str = Field(min_length=1)


StateT = TypeVar("StateT", bound=Enum)


class Dimension(BaseModel, Generic[StateT]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: StateT
    evidence: tuple[ObservationEvidence, ...] = ()


class AgentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime
    definition: Dimension[DefinitionState]
    process: Dimension[ProcessState]
    turn: Dimension[TurnState]
    communication: Dimension[CommunicationState]
    progress: Dimension[ProgressState]


_ACTIVE_TURN_RE = re.compile(
    r"\b(musing|reasoning|processing|deliberating|ruminating|formulating|thinking)\b",
    re.IGNORECASE,
)
_ELAPSED_RE = re.compile(r"[·|]\s*(\d+h\s*)?(\d+m\s*)?\d+s\b")


def _evidence(source: str, detail: str) -> tuple[ObservationEvidence, ...]:
    return (ObservationEvidence(source=source, detail=detail),)


def _turn_observation(status: dict) -> Dimension[TurnState]:
    control = status.get("runtime_control") or {}
    phase = str(control.get("current_phase") or "").strip().lower()
    phase_map = {
        "queued": TurnState.QUEUED,
        "prefilling": TurnState.PREFILLING,
        "reasoning": TurnState.REASONING,
        "tool_running": TurnState.TOOL_RUNNING,
        "decoding": TurnState.DECODING,
        "idle": TurnState.IDLE,
    }
    if phase in phase_map:
        return Dimension(
            state=phase_map[phase],
            evidence=_evidence("runtime_control.current_phase", phase),
        )

    pane_state = str(status.get("pane_state") or "unknown")
    if pane_state in {"y_n_prompt", "auth_error", "login_url", "limit_reached"}:
        return Dimension(
            state=TurnState.BLOCKED,
            evidence=_evidence("terminal.pane_state", pane_state),
        )

    pane = str(status.get("pane_text") or "")
    tail = pane[-2500:]
    active = _ACTIVE_TURN_RE.search(tail)
    if active and _ELAPSED_RE.search(tail):
        return Dimension(
            state=TurnState.REASONING,
            evidence=_evidence("terminal.status_line", active.group(1).lower()),
        )
    if re.search(r"─\s*ready\s*[│|]", tail, re.IGNORECASE):
        return Dimension(
            state=TurnState.IDLE,
            evidence=_evidence("terminal.status_line", "ready"),
        )
    if status.get("current_tool"):
        return Dimension(
            state=TurnState.TOOL_RUNNING,
            evidence=_evidence("transcript.current_tool", str(status["current_tool"])),
        )
    return Dimension(
        state=TurnState.UNKNOWN,
        evidence=_evidence("observation", "no authoritative turn-phase signal"),
    )


def build_agent_observation(
    status: dict, *, definition_state: DefinitionState
) -> dict:
    """Return the validated observation projection for ``agent_status``."""
    definition = Dimension(
        state=definition_state,
        evidence=_evidence(
            "spec.loader",
            {
                DefinitionState.VALID: "spec loaded and validated",
                DefinitionState.INVALID: "spec exists but did not validate",
                DefinitionState.MISSING: "no local spec was observed",
            }[definition_state],
        ),
    )

    liveness = status.get("liveness") or {}
    verdict = str(liveness.get("verdict") or "unknown").lower()
    process_map = {
        "alive": ProcessState.ALIVE,
        "dead": ProcessState.EXITED,
        "unknown": ProcessState.UNKNOWN,
    }
    process = Dimension(
        state=process_map.get(verdict, ProcessState.UNKNOWN),
        evidence=_evidence("liveness.verdict", verdict),
    )

    delivery = next(
        (
            item
            for item in liveness.get("evidence", [])
            if item.get("source") == "delivery"
        ),
        None,
    )
    if delivery is None:
        communication = Dimension(
            state=CommunicationState.UNKNOWN,
            evidence=_evidence("liveness.delivery", "no delivery observation"),
        )
    else:
        delivery_verdict = str(delivery.get("verdict") or "unknown").lower()
        communication_map = {
            "alive": CommunicationState.REACHABLE,
            "dead": CommunicationState.UNREACHABLE,
        }
        communication = Dimension(
            state=communication_map.get(
                delivery_verdict, CommunicationState.UNKNOWN
            ),
            evidence=_evidence(
                "liveness.delivery",
                str(delivery.get("detail") or delivery_verdict),
            ),
        )

    observation = AgentObservation(
        observed_at=datetime.now(timezone.utc),
        definition=definition,
        process=process,
        turn=_turn_observation(status),
        communication=communication,
        progress=Dimension(
            state=ProgressState.UNKNOWN,
            evidence=_evidence(
                "observation",
                "one snapshot cannot establish progress; compare successive observations",
            ),
        ),
    )
    return observation.model_dump(mode="json")


__all__ = [
    "AgentObservation",
    "CommunicationState",
    "DefinitionState",
    "ProcessState",
    "ProgressState",
    "TurnState",
    "build_agent_observation",
]
