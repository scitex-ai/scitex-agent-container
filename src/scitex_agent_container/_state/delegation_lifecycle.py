"""Durable A2A message/delegation lifecycle on the shared PostgreSQL store.

The existing ``dispatches`` ledger remains the compatibility/observability
surface.  This store is the protocol contract: every stage is an append-only
row keyed by ``(dispatch_id, stage)`` so later progress cannot erase evidence
that storage, queueing, or observation happened earlier.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Callable

from scitex_dev.status import is_exchange_id, new_exchange_id

STORE_NAME = "a2a_lifecycle"

STORE_ACCEPTED = "store_accepted"
AGENT_QUEUE_ACCEPTED = "agent_queue_accepted"
AGENT_OBSERVED = "agent_observed"
COMPLETED = "completed"
FAILED = "failed"
RECEIPT_NUDGE_SENT = "receipt_nudge_sent"
PROGRESS_NUDGE_SENT = "progress_nudge_sent"
FAILURE_REPORTED = "failure_reported"

TERMINAL_STAGES = frozenset({COMPLETED, FAILED})
VALID_STAGES = frozenset(
    {
        STORE_ACCEPTED,
        AGENT_QUEUE_ACCEPTED,
        AGENT_OBSERVED,
        COMPLETED,
        FAILED,
        RECEIPT_NUDGE_SENT,
        PROGRESS_NUDGE_SENT,
        FAILURE_REPORTED,
    }
)
VALID_KINDS = frozenset({"message", "delegation", "reply", "ack", "system"})


def new_correlation_id() -> str:
    """Return a canonical SciTeX exchange id for correlation/lineage."""
    return new_exchange_id()


def validated_reverse_route(*, sender: str, listen_url: str) -> str:
    """Build the only supported reverse route, rejecting ambiguous inputs."""
    from urllib.parse import quote, urlsplit

    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("reverse route requires a non-empty sender identity")
    parsed = urlsplit(listen_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("reverse route requires an absolute http(s) listen URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("reverse route must not embed credentials")
    return f"{listen_url.rstrip('/')}/agents/{quote(sender, safe='')}/message:send"


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=True,
    )


def _fact(kind: Any, *, required: bool = False, indexed: bool = False) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=MergeRule.IMMUTABLE,
        indexed=indexed,
    )


def _schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=STORE_NAME,
        fields={
            "dispatch_id": _ident(FieldKind.TEXT),
            "stage": _ident(FieldKind.TEXT),
            "correlation_id": _fact(FieldKind.TEXT, required=True, indexed=True),
            "lineage_id": _fact(FieldKind.TEXT, required=True, indexed=True),
            "parent_dispatch_id": _fact(FieldKind.TEXT),
            "kind": _fact(FieldKind.TEXT, required=True),
            "sender": _fact(FieldKind.TEXT, required=True, indexed=True),
            "assignee": _fact(FieldKind.TEXT, required=True, indexed=True),
            "task_responsibility": _fact(FieldKind.TEXT, required=True),
            "execution_responsibility": _fact(FieldKind.TEXT, required=True),
            "supervision_responsibility": _fact(FieldKind.TEXT, required=True),
            "reverse_route": _fact(FieldKind.TEXT, required=True),
            "reverse_route_validated_at": _fact(FieldKind.REAL, required=True),
            "receipt_deadline_at": _fact(FieldKind.REAL, required=True),
            "progress_deadline_at": _fact(FieldKind.REAL, required=True),
            "event_at": _fact(FieldKind.REAL, required=True),
            "detail": _fact(FieldKind.TEXT),
        },
    )


def _store() -> Any:
    from scitex_dev.store import Store, WriterPolicy, host_store

    return Store(
        host_store(pkg="scitex_agent_container", name=STORE_NAME),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor="scitex-agent-container",
    )


def _validate_id(label: str, value: str) -> None:
    if not is_exchange_id(value):
        raise ValueError(f"{label} must be a canonical xch_ exchange id")


def _rows_for(store: Any, dispatch_id: str) -> list[dict[str, Any]]:
    return [
        dict(row.values)
        for row in store.rows()
        if row.values.get("dispatch_id") == dispatch_id
    ]


def open_lifecycle(
    *,
    dispatch_id: str,
    correlation_id: str,
    lineage_id: str,
    kind: str,
    sender: str,
    assignee: str,
    reverse_route: str,
    receipt_deadline_at: float,
    progress_deadline_at: float,
    parent_dispatch_id: str | None = None,
    now: float | None = None,
    _store_factory: Callable[[], Any] = _store,
) -> None:
    """Persist ``store_accepted`` and the responsibility-transfer contract."""
    _validate_id("correlation_id", correlation_id)
    _validate_id("lineage_id", lineage_id)
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown outbound kind {kind!r}")
    if not sender or not assignee:
        raise ValueError("sender and assignee must be non-empty")
    if not reverse_route.startswith(("http://", "https://")):
        raise ValueError("reverse_route must be an absolute http(s) URL")
    accepted_at = time.time() if now is None else float(now)
    if receipt_deadline_at <= accepted_at:
        raise ValueError("receipt deadline must be after store acceptance")
    if progress_deadline_at < receipt_deadline_at:
        raise ValueError("progress deadline must not precede receipt deadline")

    values = {
        "dispatch_id": dispatch_id,
        "stage": STORE_ACCEPTED,
        "correlation_id": correlation_id,
        "lineage_id": lineage_id,
        "parent_dispatch_id": parent_dispatch_id,
        "kind": kind,
        "sender": sender,
        "assignee": assignee,
        # Delegation transfers task + execution. The sender always retains
        # supervision; ordinary messages use the same explicit ownership.
        "task_responsibility": assignee,
        "execution_responsibility": assignee,
        "supervision_responsibility": sender,
        "reverse_route": reverse_route,
        "reverse_route_validated_at": accepted_at,
        "receipt_deadline_at": float(receipt_deadline_at),
        "progress_deadline_at": float(progress_deadline_at),
        "event_at": accepted_at,
        "detail": "outbound contract persisted before transport",
    }
    from scitex_dev.store import NEW_RECORD

    store = _store_factory()
    try:
        store.put(values, expected_revision=NEW_RECORD)
    finally:
        store.close()


def record_stage(
    dispatch_id: str,
    stage: str,
    *,
    detail: str | None = None,
    now: float | None = None,
    _store_factory: Callable[[], Any] = _store,
) -> bool:
    """Append one stage, idempotently; terminal outcomes are immutable."""
    if stage not in VALID_STAGES or stage == STORE_ACCEPTED:
        raise ValueError(f"invalid follow-up lifecycle stage {stage!r}")
    from scitex_dev.store import NEW_RECORD

    store = _store_factory()
    try:
        rows = _rows_for(store, dispatch_id)
        opened = next((r for r in rows if r.get("stage") == STORE_ACCEPTED), None)
        if opened is None:
            raise LookupError(f"no lifecycle contract for dispatch {dispatch_id!r}")
        if any(r.get("stage") == stage for r in rows):
            return False
        terminal = {str(r.get("stage")) for r in rows} & TERMINAL_STAGES
        if terminal and stage not in {FAILURE_REPORTED}:
            raise RuntimeError(
                f"dispatch {dispatch_id!r} is terminal at {sorted(terminal)!r}"
            )
        values = dict(opened)
        values["stage"] = stage
        values["event_at"] = time.time() if now is None else float(now)
        values["detail"] = detail
        store.put(values, expected_revision=NEW_RECORD)
        return True
    finally:
        store.close()


def lifecycle_rows(
    *,
    sender: str | None = None,
    _store_factory: Callable[[], Any] = _store,
) -> list[dict[str, Any]]:
    store = _store_factory()
    try:
        rows = [dict(row.values) for row in store.rows()]
    finally:
        store.close()
    if sender is not None:
        rows = [row for row in rows if row.get("sender") == sender]
    return sorted(rows, key=lambda row: float(row.get("event_at") or 0.0))


def due_supervision_actions(
    *,
    now: float | None = None,
    _store_factory: Callable[[], Any] = _store,
) -> list[dict[str, Any]]:
    """Return one durable, deduplicated action per expired obligation."""
    current = time.time() if now is None else float(now)
    rows = lifecycle_rows(_store_factory=_store_factory)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["dispatch_id"]), []).append(row)
    actions: list[dict[str, Any]] = []
    for dispatch_id, events in grouped.items():
        opened = next(e for e in events if e["stage"] == STORE_ACCEPTED)
        stages = {str(e["stage"]) for e in events}
        if FAILED in stages and FAILURE_REPORTED not in stages:
            actions.append({**opened, "action": FAILURE_REPORTED})
            continue
        if stages & TERMINAL_STAGES:
            continue
        if (
            AGENT_OBSERVED not in stages
            and current >= float(opened["receipt_deadline_at"])
            and RECEIPT_NUDGE_SENT not in stages
        ):
            actions.append({**opened, "action": RECEIPT_NUDGE_SENT})
            continue
        if (
            AGENT_OBSERVED in stages
            and current >= float(opened["progress_deadline_at"])
            and PROGRESS_NUDGE_SENT not in stages
        ):
            actions.append({**opened, "action": PROGRESS_NUDGE_SENT})
    return actions


__all__ = [
    "AGENT_OBSERVED",
    "AGENT_QUEUE_ACCEPTED",
    "COMPLETED",
    "FAILED",
    "FAILURE_REPORTED",
    "PROGRESS_NUDGE_SENT",
    "RECEIPT_NUDGE_SENT",
    "STORE_ACCEPTED",
    "due_supervision_actions",
    "lifecycle_rows",
    "new_correlation_id",
    "open_lifecycle",
    "record_stage",
    "validated_reverse_route",
]
