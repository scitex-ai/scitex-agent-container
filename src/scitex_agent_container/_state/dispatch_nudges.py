"""Persistent bounded nudge state machine for missing model-authored ACKs."""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Protocol

STORE_NAME = "dispatch_nudges"
_STOP_STATUSES = {
    "agentic_acked",
    "in_progress",
    "completed",
    "failed",
    "timeout",
    "cancelled",
}


@dataclass(frozen=True)
class NudgeRecord:
    agent: str
    dispatch_id: str
    target: str
    created_at: float
    attempts: int
    last_nudge: float | None
    next_nudge_at: float
    deadline: float
    state: str = "pending"
    stopped_at: float | None = None
    escalated_at: float | None = None


class NudgeRepository(Protocol):
    def get(self, agent: str, dispatch_id: str) -> NudgeRecord | None: ...

    def put(self, record: NudgeRecord) -> None: ...

    def pending(self, agent: str) -> list[NudgeRecord]: ...


def schedule_nudge(
    repo: NudgeRepository,
    *,
    agent: str,
    dispatch_id: str,
    target: str,
    now: float,
    initial_delay_s: float,
    deadline_s: float,
) -> NudgeRecord:
    """Idempotently persist the first nudge/deadline before any worker tick."""
    if initial_delay_s <= 0 or deadline_s <= 0:
        raise ValueError("nudge delays must be positive")
    existing = repo.get(agent, dispatch_id)
    if existing is not None:
        return existing
    record = NudgeRecord(
        agent=agent,
        dispatch_id=dispatch_id,
        target=target,
        created_at=float(now),
        attempts=0,
        last_nudge=None,
        next_nudge_at=float(now) + float(initial_delay_s),
        deadline=float(now) + float(deadline_s),
    )
    repo.put(record)
    return record


def tick_nudges(
    repo: NudgeRepository,
    *,
    agent: str,
    now: float,
    status_of: Callable[[str], str | None],
    send_nudge: Callable[[NudgeRecord], None],
    escalate: Callable[[NudgeRecord], None],
    initial_delay_s: float,
    max_delay_s: float,
) -> list[tuple[str, str]]:
    """Advance due rows once; persistence happens before each side effect.

    Pre-persisting attempts/escalation is the restart-dedup boundary: a crash
    after the outbound call cannot resend the same nudge window forever.
    """
    if initial_delay_s <= 0 or max_delay_s < initial_delay_s:
        raise ValueError("invalid nudge backoff bounds")
    actions: list[tuple[str, str]] = []
    for record in repo.pending(agent):
        status = status_of(record.dispatch_id)
        if status in _STOP_STATUSES:
            stopped = replace(record, state="stopped", stopped_at=float(now))
            repo.put(stopped)
            actions.append((record.dispatch_id, "stopped"))
            continue
        if now >= record.deadline:
            escalated = replace(record, state="escalated", escalated_at=float(now))
            repo.put(escalated)
            escalate(escalated)
            actions.append((record.dispatch_id, "escalated"))
            continue
        if now < record.next_nudge_at:
            continue
        attempts = record.attempts + 1
        delay = min(float(max_delay_s), float(initial_delay_s) * (2**attempts))
        attempted = replace(
            record,
            attempts=attempts,
            last_nudge=float(now),
            next_nudge_at=float(now) + delay,
        )
        repo.put(attempted)
        send_nudge(attempted)
        actions.append((record.dispatch_id, "nudged"))
    return actions


def _policy(kind: Any, *, identity: bool = False, moving: bool = False) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY if identity else FieldRole.DATA,
        required=identity,
        merge=MergeRule.LAST_WRITER_WINS if moving else MergeRule.IMMUTABLE,
        indexed=False,
    )


def _schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=STORE_NAME,
        fields={
            "agent": _policy(FieldKind.TEXT, identity=True),
            "dispatch_id": _policy(FieldKind.TEXT, identity=True),
            "target": _policy(FieldKind.TEXT),
            "created_at": _policy(FieldKind.REAL),
            "attempts": _policy(FieldKind.INTEGER, moving=True),
            "last_nudge": _policy(FieldKind.REAL, moving=True),
            "next_nudge_at": _policy(FieldKind.REAL, moving=True),
            "deadline": _policy(FieldKind.REAL),
            "state": _policy(FieldKind.TEXT, moving=True),
            "stopped_at": _policy(FieldKind.REAL, moving=True),
            "escalated_at": _policy(FieldKind.REAL, moving=True),
        },
    )


def _open_store():
    from scitex_dev.store import Store, WriterPolicy, host_store

    return Store(
        host_store(pkg="scitex_agent_container", name=STORE_NAME),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor="scitex-agent-container",
    )


def _from_values(values: dict[str, Any]) -> NudgeRecord:
    return NudgeRecord(
        agent=str(values["agent"]),
        dispatch_id=str(values["dispatch_id"]),
        target=str(values["target"]),
        created_at=float(values["created_at"]),
        attempts=int(values["attempts"]),
        last_nudge=(
            float(values["last_nudge"])
            if values.get("last_nudge") is not None
            else None
        ),
        next_nudge_at=float(values["next_nudge_at"]),
        deadline=float(values["deadline"]),
        state=str(values["state"]),
        stopped_at=(
            float(values["stopped_at"])
            if values.get("stopped_at") is not None
            else None
        ),
        escalated_at=(
            float(values["escalated_at"])
            if values.get("escalated_at") is not None
            else None
        ),
    )


class PostgresNudgeRepository:
    """Fleet-shared durable repository; no local-file fallback."""

    def get(self, agent: str, dispatch_id: str) -> NudgeRecord | None:
        store = _open_store()
        try:
            row = store.get({"agent": agent, "dispatch_id": dispatch_id})
            return _from_values(dict(row.values)) if row is not None else None
        finally:
            store.close()

    def put(self, record: NudgeRecord) -> None:
        from scitex_dev.store import ANY_REVISION, NEW_RECORD

        store = _open_store()
        try:
            existing = store.get(
                {"agent": record.agent, "dispatch_id": record.dispatch_id}
            )
            store.put(
                asdict(record),
                expected_revision=NEW_RECORD if existing is None else ANY_REVISION,
            )
        finally:
            store.close()

    def pending(self, agent: str) -> list[NudgeRecord]:
        store = _open_store()
        try:
            rows = [
                _from_values(dict(row.values))
                for row in store.rows()
                if row.values.get("agent") == agent
                and row.values.get("state") == "pending"
            ]
        finally:
            store.close()
        return sorted(rows, key=lambda row: (row.next_nudge_at, row.dispatch_id))


__all__ = [
    "NudgeRecord",
    "NudgeRepository",
    "PostgresNudgeRepository",
    "schedule_nudge",
    "tick_nudges",
]
