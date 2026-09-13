"""Canonical ``scitex_dev.status`` ledger adapter for TUI turn delivery."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from scitex_dev.status import (
    StatusCode,
    is_exchange_id,
    ledger_record,
    ledger_schema,
    new_exchange_id,
)


class TurnExchangeStoreUnavailable(RuntimeError):
    """The shared protocol ledger exists but this bridge cannot use it."""


def _privilege_failure(exc: BaseException) -> bool:
    """True for PostgreSQL ``insufficient_privilege`` without importing psycopg."""
    return getattr(exc, "sqlstate", None) == "42501"


def _access_error(exc: BaseException) -> TurnExchangeStoreUnavailable:
    role = os.environ.get("PGUSER", "<libpq default>")
    return TurnExchangeStoreUnavailable(
        "the SciTeX protocol status_exchanges store is not usable by "
        f"PGUSER={role!r}: PostgreSQL denied access. The database provisioner "
        "must make scitex_store_owner own public.status_exchanges_{rows,oplog,"
        "cursor,identity} and grant SELECT, INSERT, UPDATE, DELETE on those "
        "tables to scitex_rw. SAC will not change shared-database ownership "
        "from an agent process. Re-run the bridge only after an owner-level "
        "catalogue and DML probe both pass."
    )


def _provision_error(exc: BaseException) -> TurnExchangeStoreUnavailable:
    """Translate scitex-dev's managed-DDL refusal into an operator action."""
    role = os.environ.get("PGUSER", "<libpq default>")
    return TurnExchangeStoreUnavailable(
        "the SciTeX protocol status_exchanges store requires privileged "
        f"provisioning before PGUSER={role!r} may open it: {exc} "
        "Use scitex_dev.store.host_store(pkg='dev', "
        "name='status_exchanges') with ledger_schema(), then run "
        "provision_store_acl(target, schema) through the authorized migration "
        "identity. Verify the result with inspect_store_acl(target, schema). "
        "SAC will not create, re-own, or grant shared Store tables from an "
        "agent process."
    )


def _store(*, _store_type: Callable[..., Any] | None = None):
    from scitex_dev.store import (
        Store,
        StoreProvisionError,
        WriterPolicy,
        host_store,
    )

    try:
        store_type = Store if _store_type is None else _store_type
        return store_type(
            host_store(pkg="dev", name="status_exchanges"),
            ledger_schema(),
            node=socket.gethostname(),
            writer_policy=WriterPolicy.MULTI_WRITER,
            actor="scitex-agent-container",
        )
    except Exception as exc:
        if isinstance(exc, StoreProvisionError):
            raise _provision_error(exc) from exc
        if _privilege_failure(exc):
            raise _access_error(exc) from exc
        raise


def preflight_turn_exchange_store() -> None:
    """Prove the canonical ledger can be opened before the HTTP bridge binds."""
    store = _store()
    store.close()


def open_turn_exchange(
    *,
    agent: str,
    probe_url: str,
    exchange_id: str | None = None,
    delivery_id: str | None = None,
    initiator: str | None = None,
    operation: str = "cards.dm.delivery",
    _store_factory: Callable[[], Any] = _store,
) -> tuple[str, str]:
    """Adopt Cards' exchange or persist one stable legacy-delivery exchange.

    A Cards producer that supplied an exchange owns the identity.  Older
    notifications did not, so their opaque delivery id is fingerprinted into
    an immutable, indexed operation name.  Looking that operation up before
    minting means every retry (including one after a bridge restart) adopts
    the same durable exchange instead of producing an unbounded trail of
    terminal attempt rows.
    """
    from scitex_dev.store import NEW_RECORD, Query, eq

    if exchange_id is not None:
        if not delivery_id:
            raise ValueError(
                "an adopted Cards exchange requires its durable delivery id"
            )
        if not initiator:
            raise ValueError(
                "an adopted Cards exchange requires its authenticated initiator"
            )
        if not is_exchange_id(exchange_id):
            raise ValueError("the supplied exchange_id is not canonical")
        store = _store_factory()
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
        if values.get("initiator") != initiator:
            raise PermissionError(
                "the supplied Cards exchange belongs to another initiator"
            )
        if values.get("operation") != operation:
            raise PermissionError(
                "the supplied Cards exchange belongs to another operation"
            )
        _reject_final_failure(values)
        return exchange_id, str(values["opened_at"])

    operation = "cards.notification.terminal_delivery"
    if delivery_id is not None:
        fingerprint = sha256(f"{agent}\0{delivery_id}".encode()).hexdigest()
        operation = f"cards.notification.visible_delivery.sha256:{fingerprint}"
        store = _store_factory()
        try:
            found = store.search(
                Query().where(
                    eq("operation", operation),
                    eq("responder", f"scitex-agent-container/{agent}"),
                )
            )
        finally:
            store.close()
        if len(found) > 1:
            raise RuntimeError(
                "more than one exchange is associated with this delivery id; "
                "refusing to guess which durable operation owns the retry"
            )
        if found:
            values = dict(found[0].values)
            _reject_final_failure(values)
            return str(values["exchange_id"]), str(values["opened_at"])

    exchange_id = new_exchange_id()
    opened_at = datetime.now(timezone.utc).isoformat()
    status = StatusCode(
        kind="http",
        code=202,
        message=(
            f"turn delivery accepted for {agent!r}; poll `{probe_url}/{exchange_id}` "
            "for the separately recorded harness-visibility result"
        ),
    )
    store = _store_factory()
    try:
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator="scitex-cards",
                responder=f"scitex-agent-container/{agent}",
                operation=operation,
                status=status,
                opened_at=opened_at,
            ),
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    return exchange_id, opened_at


def _reject_final_failure(values: dict[str, Any]) -> None:
    """Never reopen a terminal failure as if it were mutable progress."""
    if values.get("final") and not (
        values.get("kind") == "http" and values.get("code") == 200
    ):
        raise RuntimeError(
            f"exchange {values.get('exchange_id')!r} is already final at "
            f"{values.get('kind')}/{values.get('code')}; the producer must issue "
            "a new exchange before delivery can be retried"
        )


def finish_turn_exchange(
    exchange_id: str,
    *,
    agent: str,
    opened_at: str,
    status: StatusCode,
    _store_factory: Callable[[], Any] = _store,
) -> None:
    """Advance one exchange, refusing every change after a final status."""
    from scitex_dev.store import ANY_REVISION

    store = _store_factory()
    try:
        existing = store.get({"exchange_id": exchange_id})
        if existing is None:
            raise LookupError("the accepted exchange disappeared before completion")
        values = dict(existing.values)
        if values.get("final"):
            same_status = (
                values.get("kind"),
                values.get("code"),
                values.get("message"),
            ) == (status.kind, status.code, status.message)
            if same_status:
                return
            raise RuntimeError(
                f"exchange {exchange_id!r} is already final at "
                f"{values.get('kind')}/{values.get('code')}; refusing to rewrite "
                f"it as {status.kind}/{status.code}"
            )
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


def read_turn_exchange(
    exchange_id: str, *, _store_factory: Callable[[], Any] = _store
) -> dict[str, Any] | None:
    """Read one exchange's current canonical row."""
    store = _store_factory()
    try:
        row = store.get({"exchange_id": exchange_id})
        return None if row is None else dict(row.values)
    finally:
        store.close()


__all__ = [
    "TurnExchangeStoreUnavailable",
    "finish_turn_exchange",
    "open_turn_exchange",
    "preflight_turn_exchange_store",
    "read_turn_exchange",
]
