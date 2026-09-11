"""Durable, harness-neutral inbox for messages accepted by SAC.

The public listener and per-incarnation adapters may change, but an accepted
message must survive a busy agent and a bridge restart.  This module stores
that stable contract in ``scitex_dev.store`` (PostgreSQL): payload, sender,
target, acceptance time, and delivery state.  Harness adapters only claim a
message and report whether they delivered it.
"""

from __future__ import annotations

import socket
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scitex_dev.store import Row, Store

STORE_NAME = "message_inbox"
ACTOR = "scitex-agent-container"

STATUS_ACCEPTED = "accepted"
STATUS_DELIVERING = "delivering"
STATUS_DELIVERED = "delivered"
VALID_STATUSES = (STATUS_ACCEPTED, STATUS_DELIVERING, STATUS_DELIVERED)

__all__ = [
    "MessageConflictError",
    "STATUS_ACCEPTED",
    "STATUS_DELIVERED",
    "STATUS_DELIVERING",
    "STORE_NAME",
    "accept_message",
    "claim_next_message",
    "list_messages",
    "mark_delivered",
    "release_message",
]


class MessageConflictError(ValueError):
    """One id was retried with a different immutable message envelope."""


def _schema() -> Any:
    from .._store_plugin import MESSAGE_INBOX

    return MESSAGE_INBOX


def _target() -> Any:
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def _open_store() -> "Store":
    from scitex_dev.store import Store, WriterPolicy

    return Store(
        _target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


def _values(row: "Row") -> dict[str, Any]:
    return dict(row.values)


def accept_message(
    *,
    target_agent: str,
    payload: str,
    sender_agent: str | None = None,
    message_id: str | None = None,
    content_type: str = "text/plain",
    accepted_at: float | None = None,
) -> dict[str, Any]:
    """Persist one accepted message and return its neutral receipt.

    A caller-supplied ``message_id`` is an idempotency key.  This is normally
    the A2A dispatch id, so a transport retry cannot enqueue duplicate work.
    """
    if not target_agent or not payload.strip():
        raise ValueError("target_agent and non-empty payload are required")

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    mid = (message_id or "").strip() or str(uuid.uuid4())
    record = {
        "message_id": mid,
        "target_agent": target_agent,
        "sender_agent": sender_agent or "",
        "payload": payload,
        "content_type": content_type,
        "status": STATUS_ACCEPTED,
        "accepted_at": float(accepted_at if accepted_at is not None else time.time()),
        "attempts": 0,
    }
    store = _open_store()
    try:
        try:
            store.put(record, expected_revision=NEW_RECORD)
        except RevisionMismatchError:
            existing = store.get({"message_id": mid})
            if existing is None:
                raise
            values = _values(existing)
            if (
                values.get("target_agent") != target_agent
                or values.get("payload") != payload
                or values.get("sender_agent", "") != (sender_agent or "")
                or values.get("content_type") != content_type
            ):
                raise MessageConflictError(
                    f"message_id {mid!r} already identifies different content"
                )
            record = values
        return {
            "message_id": mid,
            "status": record.get("status", STATUS_ACCEPTED),
            "accepted_at": record.get("accepted_at"),
        }
    finally:
        store.close()


def _deliverable_rows(
    store: "Store", *, target_agent: str, stale_before: float
) -> list["Row"]:
    """Read only this target's accepted and stale-delivering candidates."""
    from scitex_dev.store import Query, eq, lte

    accepted = Query().where(
        eq("target_agent", target_agent),
        eq("status", STATUS_ACCEPTED),
    )
    stale = Query().where(
        eq("target_agent", target_agent),
        eq("status", STATUS_DELIVERING),
        lte("last_attempt_at", stale_before),
    )
    # Two indexed, target-scoped reads replace the old store.rows() scan. The
    # store query vocabulary cannot express (A) OR (B AND C) in one flat
    # criterion, so merge the two disjoint status sets in memory.
    return [*store.search(accepted), *store.search(stale)]


def claim_next_message(
    *,
    target_agent: str,
    stale_after_seconds: float = 120.0,
    now: float | None = None,
    lease_owner: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the oldest deliverable message for ``target_agent``.

    A stale ``delivering`` claim is eligible again, which recovers a message
    after an adapter process dies between claim and acknowledgement.
    """
    from scitex_dev.store import RevisionMismatchError

    current = float(now if now is not None else time.time())
    owner = (lease_owner or "").strip() or str(uuid.uuid4())
    stale_before = current - float(stale_after_seconds)
    store = _open_store()
    try:
        candidates = _deliverable_rows(
            store, target_agent=target_agent, stale_before=stale_before
        )
        candidates.sort(key=lambda row: float(row.values.get("accepted_at") or 0.0))
        for row in candidates:
            values = _values(row)
            revision = store.revision(row.key)
            if revision is None:
                continue
            updated = {
                **values,
                "status": STATUS_DELIVERING,
                "attempts": int(values.get("attempts") or 0) + 1,
                "last_attempt_at": current,
                "lease_owner": owner,
            }
            try:
                store.put(updated, expected_revision=revision)
            except RevisionMismatchError:
                continue
            return updated
        return None
    finally:
        store.close()


def _update_claim(
    message_id: str, *, expected_lease_owner: str, **changes: Any
) -> bool:
    """CAS one claimed message only while this consumer owns its lease."""
    from scitex_dev.store import RevisionMismatchError

    store = _open_store()
    try:
        row = store.get({"message_id": message_id})
        if row is None:
            return False
        values = _values(row)
        if (
            values.get("status") != STATUS_DELIVERING
            or values.get("lease_owner") != expected_lease_owner
        ):
            return False
        revision = store.revision(row.key)
        if revision is None:
            return False
        try:
            store.put({**values, **changes}, expected_revision=revision)
        except RevisionMismatchError:
            return False
        return True
    finally:
        store.close()


def release_message(message_id: str, *, lease_owner: str, error: str = "") -> bool:
    """Return a failed delivery claim to the accepted queue."""
    return _update_claim(
        message_id,
        expected_lease_owner=lease_owner,
        status=STATUS_ACCEPTED,
        lease_owner="",
        last_error=error,
    )


def mark_delivered(
    message_id: str,
    *,
    incarnation_id: str = "",
    delivered_at: float | None = None,
    lease_owner: str,
) -> bool:
    """Acknowledge adapter delivery for one message."""
    return _update_claim(
        message_id,
        expected_lease_owner=lease_owner,
        status=STATUS_DELIVERED,
        lease_owner="",
        delivered_at=float(delivered_at if delivered_at is not None else time.time()),
        last_error="",
        incarnation_id=incarnation_id,
    )


def list_messages(
    *, target_agent: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Return inbox rows newest-first for observation and tests."""
    from scitex_dev.store import Query, eq

    query = Query()
    if target_agent:
        query = query.where(eq("target_agent", target_agent))
    if status:
        query = query.where(eq("status", status))
    query = query.ordered_by("accepted_at").limited(int(limit))
    store = _open_store()
    try:
        rows = [_values(row) for row in store.search(query)]
    finally:
        store.close()
    return rows
