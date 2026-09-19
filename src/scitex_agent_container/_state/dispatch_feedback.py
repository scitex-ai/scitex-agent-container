"""Model-authored acknowledgement and progress bound to dispatch nonces."""

from __future__ import annotations

import socket
import time
from typing import Any

from .dispatch_ledger import (
    STATUS_AGENTIC_ACKED,
    STATUS_COMPLETED,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_REACTED,
    STATUS_SENT,
    get_dispatch,
    update_dispatch_status,
)

STORE_NAME = "dispatch_feedback"
SUMMARY_LIMIT = 500
OWNER_LIMIT = 120
PROGRESS_STATUSES = (STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_FAILED)
_PRE_AGENTIC = (STATUS_SENT, STATUS_DELIVERED, STATUS_REACTED)


def _policy(kind: Any, *, identity: bool = False, moving: bool = False) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY if identity else FieldRole.DATA,
        required=identity,
        merge=MergeRule.IMMUTABLE if identity or not moving else MergeRule.LAST_WRITER_WINS,
        indexed=False,
    )


def _schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=STORE_NAME,
        fields={
            "agent": _policy(FieldKind.TEXT, identity=True),
            "dispatch_id": _policy(FieldKind.TEXT, identity=True),
            "peer": _policy(FieldKind.TEXT),
            "understood": _policy(FieldKind.TEXT),
            "owner": _policy(FieldKind.TEXT),
            "next_checkpoint": _policy(FieldKind.TEXT),
            "acked_at": _policy(FieldKind.REAL),
            "progress_status": _policy(FieldKind.TEXT, moving=True),
            "progress_summary": _policy(FieldKind.TEXT, moving=True),
            "blocker": _policy(FieldKind.TEXT, moving=True),
            "progress_at": _policy(FieldKind.REAL, moving=True),
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


def _owner(row: dict[str, Any]) -> str:
    """Use the row's immutable owner; ``agent=`` lookup is only a fast path."""
    return str(row.get("agent") or "")


def _feedback(dispatch_id: str, agent: str) -> dict[str, Any] | None:
    store = _open_store()
    try:
        row = store.get({"agent": agent, "dispatch_id": dispatch_id})
        return dict(row.values) if row is not None else None
    finally:
        store.close()


def _bounded(name: str, value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _put(values: dict[str, Any], *, new: bool) -> dict[str, Any]:
    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    store = _open_store()
    try:
        store.put(values, expected_revision=NEW_RECORD if new else ANY_REVISION)
        return dict(values)
    finally:
        store.close()


def _same_ack(
    existing: dict[str, Any], understood: str, owner: str, next_checkpoint: str
) -> bool:
    return (
        existing.get("understood"),
        existing.get("owner"),
        existing.get("next_checkpoint"),
    ) == (understood, owner, next_checkpoint)


def record_agentic_ack(
    dispatch_id: str,
    *,
    from_agent: str,
    understood: str,
    owner: str,
    next_checkpoint: str,
    agent: str | None = None,
) -> dict[str, Any] | None:
    """Persist an intentional ACK only for the exact nonce and expected peer.

    A wrong/stale nonce, wrong peer, or conflicting replay returns ``None`` and
    leaves both stores unchanged. An exact replay returns the existing row.
    """
    understood = _bounded("understood", understood, SUMMARY_LIMIT)
    owner = _bounded("owner", owner, OWNER_LIMIT)
    next_checkpoint = _bounded("next_checkpoint", next_checkpoint, SUMMARY_LIMIT)
    dispatch = get_dispatch(dispatch_id, agent=agent)
    if dispatch is None or dispatch.get("to_agent") != from_agent:
        return None
    owning_agent = _owner(dispatch)
    existing = _feedback(dispatch_id, owning_agent)
    if existing is not None:
        if not _same_ack(existing, understood, owner, next_checkpoint):
            return None
        if dispatch.get("status") in _PRE_AGENTIC:
            update_dispatch_status(dispatch_id, STATUS_AGENTIC_ACKED, agent=owning_agent)
        return existing
    values = {
        "agent": owning_agent,
        "dispatch_id": dispatch_id,
        "peer": from_agent,
        "understood": understood,
        "owner": owner,
        "next_checkpoint": next_checkpoint,
        "acked_at": time.time(),
        "progress_status": None,
        "progress_summary": None,
        "blocker": None,
        "progress_at": None,
    }
    saved = _put(values, new=True)
    if dispatch.get("status") in _PRE_AGENTIC:
        update_dispatch_status(dispatch_id, STATUS_AGENTIC_ACKED, agent=owning_agent)
    return saved


def record_progress(
    dispatch_id: str,
    *,
    from_agent: str,
    status: str,
    summary: str,
    blocker: str | None = None,
    agent: str | None = None,
) -> dict[str, Any] | None:
    """Record typed progress after an agentic ACK for the same exact nonce."""
    if status not in PROGRESS_STATUSES:
        raise ValueError(f"unknown progress status {status!r}")
    summary = _bounded("summary", summary, SUMMARY_LIMIT)
    if blocker is not None:
        blocker = _bounded("blocker", blocker, SUMMARY_LIMIT)
    dispatch = get_dispatch(dispatch_id, agent=agent)
    if dispatch is None or dispatch.get("to_agent") != from_agent:
        return None
    owning_agent = _owner(dispatch)
    existing = _feedback(dispatch_id, owning_agent)
    if existing is None or existing.get("understood") is None:
        return None
    previous = dispatch.get("status")
    same = (
        existing.get("progress_status"),
        existing.get("progress_summary"),
        existing.get("blocker"),
    ) == (status, summary, blocker)
    if previous in (STATUS_COMPLETED, STATUS_FAILED):
        return existing if same else None
    values = dict(existing)
    values.update(
        progress_status=status,
        progress_summary=summary,
        blocker=blocker,
        progress_at=time.time(),
    )
    saved = _put(values, new=False)
    update_dispatch_status(dispatch_id, status, agent=owning_agent)
    return saved


def dispatch_status(
    dispatch_id: str,
    *,
    agent: str | None = None,
    agentic_ack_timeout_s: float = 120.0,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Read lifecycle + latest feedback and flag an overdue semantic ACK."""
    if agentic_ack_timeout_s < 0:
        raise ValueError("agentic_ack_timeout_s must be non-negative")
    dispatch = get_dispatch(dispatch_id, agent=agent)
    if dispatch is None:
        return None
    owning_agent = _owner(dispatch)
    age_s = max(
        0.0,
        float(now if now is not None else time.time()) - float(dispatch["ts"]),
    )
    overdue = dispatch.get("status") in _PRE_AGENTIC and age_s >= agentic_ack_timeout_s
    return {
        "dispatch_id": dispatch_id,
        "status": dispatch.get("status"),
        "age_s": age_s,
        "agentic_ack_overdue": overdue,
        "escalation": "missing_agentic_ack" if overdue else None,
        "feedback": _feedback(dispatch_id, owning_agent),
    }


__all__ = [
    "OWNER_LIMIT",
    "PROGRESS_STATUSES",
    "SUMMARY_LIMIT",
    "dispatch_status",
    "record_agentic_ack",
    "record_progress",
]
