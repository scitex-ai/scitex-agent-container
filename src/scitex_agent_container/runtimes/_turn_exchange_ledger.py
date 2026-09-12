"""Canonical ``scitex_dev.status`` ledger adapter for TUI turn delivery."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from scitex_dev.status import (
    StatusCode,
    is_exchange_id,
    ledger_record,
    ledger_schema,
    new_exchange_id,
)


def _store():
    from scitex_dev.store import Store, WriterPolicy, host_store

    return Store(
        host_store(pkg="dev", name="status_exchanges"),
        ledger_schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor="scitex-agent-container",
    )


def open_turn_exchange(
    *, agent: str, probe_url: str, exchange_id: str | None = None
) -> tuple[str, str]:
    """Adopt Cards' exchange or persist a legacy turn's immediate HTTP 202."""
    from scitex_dev.store import NEW_RECORD

    if exchange_id is not None:
        if not is_exchange_id(exchange_id):
            raise ValueError("the supplied exchange_id is not canonical")
        store = _store()
        try:
            existing = store.get({"exchange_id": exchange_id})
        finally:
            store.close()
        if existing is None:
            raise LookupError(
                "the supplied Cards exchange does not exist in status_exchanges"
            )
        values = dict(existing.values)
        expected_responders = {agent, f"scitex-agent-container/{agent}"}
        if values.get("responder") not in expected_responders:
            raise PermissionError(
                "the supplied Cards exchange belongs to another responder"
            )
        # A final 200 can still need its downstream Cards ACK retried after a
        # crash; a final 502 can be retried once the staged composer/modal is
        # gone. The ledger is a current-status row with oplog history, so SAC
        # adopts the same exchange instead of minting a second identity.
        return exchange_id, str(values["opened_at"])

    exchange_id = new_exchange_id()
    opened_at = datetime.now(timezone.utc).isoformat()
    status = StatusCode(
        kind="http",
        code=202,
        message=(
            f"turn delivery accepted for {agent!r}; poll `{probe_url}/{exchange_id}` "
            "for the separately recorded terminal-visibility result"
        ),
    )
    store = _store()
    try:
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator="scitex-cards",
                responder=f"scitex-agent-container/{agent}",
                operation="cards.notification.terminal_delivery",
                status=status,
                opened_at=opened_at,
            ),
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    return exchange_id, opened_at


def finish_turn_exchange(
    exchange_id: str, *, agent: str, opened_at: str, status: StatusCode
) -> None:
    """Conclude one accepted exchange using the shared ledger constructor."""
    from scitex_dev.store import ANY_REVISION

    store = _store()
    try:
        existing = store.get({"exchange_id": exchange_id})
        if existing is None:
            raise LookupError("the accepted exchange disappeared before completion")
        values = dict(existing.values)
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator=str(values["initiator"]),
                responder=str(values["responder"]),
                operation=str(values["operation"]),
                status=status,
                opened_at=str(values.get("opened_at") or opened_at),
            ),
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()


def read_turn_exchange(exchange_id: str) -> dict[str, Any] | None:
    """Read one exchange's current canonical row."""
    store = _store()
    try:
        row = store.get({"exchange_id": exchange_id})
        return None if row is None else dict(row.values)
    finally:
        store.close()


__all__ = ["finish_turn_exchange", "open_turn_exchange", "read_turn_exchange"]
