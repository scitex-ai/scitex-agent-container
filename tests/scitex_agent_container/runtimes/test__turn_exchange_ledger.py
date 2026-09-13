"""The TUI worker consumes scitex_dev.status's canonical exchange ledger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from scitex_dev.status import StatusCode, ledger_record, new_exchange_id

from scitex_agent_container.runtimes._turn_exchange_ledger import (
    TurnExchangeStoreUnavailable,
    _access_error,
    _privilege_failure,
    _provision_error,
    _store,
    finish_turn_exchange,
    open_turn_exchange,
    read_turn_exchange,
)


class _PrivilegeDenied(Exception):
    sqlstate = "42501"


class _MemoryLedger:
    """Small store seam: exercise state transitions without live PostgreSQL."""

    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    def get(self, key: dict) -> SimpleNamespace | None:
        values = self.values.get(key["exchange_id"])
        return None if values is None else SimpleNamespace(values=values)

    def search(self, _query: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(values=value) for value in self.values.values()]

    def put(self, values: dict, *, expected_revision: object) -> None:
        del expected_revision
        self.values[str(values["exchange_id"])] = dict(values)

    def close(self) -> None:
        return

    def factory(self) -> _MemoryLedger:
        return self


def test_postgres_insufficient_privilege_is_recognised_by_sqlstate() -> None:
    # Arrange
    error = _PrivilegeDenied()
    # Act
    recognised = _privilege_failure(error)
    # Assert
    assert recognised is True


def test_an_unrelated_store_error_is_not_misreported_as_acl_drift() -> None:
    # Arrange
    error = ConnectionError("network down")
    # Act
    recognised = _privilege_failure(error)
    # Assert
    assert recognised is False


def test_store_acl_failure_names_owner_grant_and_refuses_self_heal() -> None:
    # Arrange
    denied = _PrivilegeDenied("must be owner of table status_exchanges_oplog")
    # Act
    message = str(_access_error(denied))
    # Assert
    assert (
        "PGUSER=" in message,
        "scitex_store_owner" in message,
        "scitex_rw" in message,
        "SAC will not change shared-database ownership" in message,
    ) == (True, True, True, True)


def test_managed_store_provisioning_refusal_names_the_privileged_api() -> None:
    # Arrange
    from scitex_dev.store import StoreProvisionError

    refused = StoreProvisionError("Store 'status_exchanges' needs PostgreSQL DDL")
    # Act
    message = str(_provision_error(refused))
    # Assert
    assert (
        "provision_store_acl" in message,
        "inspect_store_acl" in message,
        "authorized migration identity" in message,
        "SAC will not create, re-own, or grant" in message,
    ) == (True, True, True, True)


def test_store_constructor_maps_managed_ddl_refusal_to_actionable_bridge_error() -> (
    None
):
    # Arrange
    from scitex_dev.store import StoreProvisionError

    def refusing_store(*_args, **_kwargs):
        raise StoreProvisionError("managed store needs PostgreSQL DDL")

    # Act
    caught = pytest.raises(TurnExchangeStoreUnavailable, match="provision_store_acl")
    # Assert
    with caught:
        _store(_store_type=refusing_store)


def test_turn_exchange_moves_from_http_202_to_final_http_200(pg_schema: str) -> None:
    # Arrange
    exchange_id, opened_at = open_turn_exchange(
        agent="scitex-hub", probe_url="/v1/exchanges"
    )
    # Act
    accepted = read_turn_exchange(exchange_id)
    finish_turn_exchange(
        exchange_id,
        agent="scitex-hub",
        opened_at=opened_at,
        status=StatusCode(
            kind="http",
            code=200,
            message="the incoming turn is visible in the Hermes transcript",
        ),
    )
    final = read_turn_exchange(exchange_id)
    # Assert
    assert (accepted["code"], accepted["final"], final["code"], final["final"]) == (
        202,
        False,
        200,
        True,
    )


def test_cards_issued_exchange_is_adopted_and_identity_is_preserved(
    pg_schema: str,
) -> None:
    # Arrange
    from scitex_dev.store import NEW_RECORD

    exchange_id = new_exchange_id(host="cards")
    opened_at = "2026-09-12T00:00:00+00:00"
    store = _store()
    try:
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator="operator",
                responder="scitex-hub",
                operation="cards.dm.delivery",
                status=StatusCode(
                    kind="http",
                    code=202,
                    message=f"accepted; poll `/v1/exchanges/{exchange_id}`",
                ),
                opened_at=opened_at,
            ),
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()

    # Act
    adopted_id, adopted_at = open_turn_exchange(
        agent="scitex-hub",
        probe_url="/v1/exchanges",
        exchange_id=exchange_id,
        delivery_id="delivery-1",
        initiator="operator",
    )
    finish_turn_exchange(
        adopted_id,
        agent="scitex-hub",
        opened_at=adopted_at,
        status=StatusCode(kind="http", code=200, message="visible in Hermes"),
    )
    final = read_turn_exchange(exchange_id)

    # Assert
    assert (
        adopted_id,
        final["initiator"],
        final["responder"],
        final["operation"],
        final["code"],
    ) == (
        exchange_id,
        "operator",
        "scitex-hub",
        "cards.dm.delivery",
        200,
    )


@pytest.mark.parametrize(
    ("initiator", "operation", "match"),
    [
        ("another-sender", "cards.dm.delivery", "another initiator"),
        ("operator", "cards.other.operation", "another operation"),
    ],
)
def test_cards_exchange_adoption_rejects_identity_mismatch(
    initiator: str, operation: str, match: str
) -> None:
    # Arrange
    memory = _MemoryLedger()
    exchange_id = new_exchange_id(host="cards")
    memory.values[exchange_id] = ledger_record(
        exchange_id=exchange_id,
        initiator="operator",
        responder="scitex-hub",
        operation="cards.dm.delivery",
        status=StatusCode(
            kind="http",
            code=202,
            message=f"accepted; poll `/v1/exchanges/{exchange_id}`",
        ),
        opened_at="2026-09-12T00:00:00+00:00",
    )
    # Act
    caught = pytest.raises(PermissionError, match=match)
    # Assert
    with caught:
        open_turn_exchange(
            agent="scitex-hub",
            probe_url="/v1/exchanges",
            exchange_id=exchange_id,
            delivery_id="notification-1",
            initiator=initiator,
            operation=operation,
            _store_factory=memory.factory,
        )


def test_legacy_delivery_reuses_one_exchange_across_failure_then_success() -> None:
    # Arrange
    memory_ledger = _MemoryLedger()
    store_factory = memory_ledger.factory
    first_id, opened_at = open_turn_exchange(
        agent="scitex-hub",
        probe_url="/v1/exchanges",
        delivery_id="n_one-durable-operation",
        _store_factory=store_factory,
    )
    finish_turn_exchange(
        first_id,
        agent="scitex-hub",
        opened_at=opened_at,
        status=StatusCode(
            kind="http",
            code=102,
            message=f"projection pending; poll `/v1/exchanges/{first_id}`",
        ),
        _store_factory=store_factory,
    )

    # Act
    retry_id, retry_opened_at = open_turn_exchange(
        agent="scitex-hub",
        probe_url="/v1/exchanges",
        delivery_id="n_one-durable-operation",
        _store_factory=store_factory,
    )
    finish_turn_exchange(
        retry_id,
        agent="scitex-hub",
        opened_at=retry_opened_at,
        status=StatusCode(kind="http", code=200, message="visible in Hermes"),
        _store_factory=store_factory,
    )
    final = read_turn_exchange(first_id, _store_factory=store_factory)

    # Assert
    assert (
        retry_id,
        retry_opened_at,
        final["code"],
        final["final"],
        len(memory_ledger.values),
    ) == (
        first_id,
        opened_at,
        200,
        True,
        1,
    )


def _final_failure(memory_ledger: _MemoryLedger) -> tuple[str, str]:
    """Create one terminal exchange in the supplied test ledger."""
    store_factory = memory_ledger.factory
    exchange_id, opened_at = open_turn_exchange(
        agent="scitex-hub",
        probe_url="/v1/exchanges",
        delivery_id="delivery-final",
        _store_factory=store_factory,
    )
    finish_turn_exchange(
        exchange_id,
        agent="scitex-hub",
        opened_at=opened_at,
        status=StatusCode(kind="http", code=502, message="terminal refusal"),
        _store_factory=store_factory,
    )
    return exchange_id, opened_at


def test_final_failure_cannot_be_reopened() -> None:
    # Arrange
    memory_ledger = _MemoryLedger()
    exchange_id, _opened_at = _final_failure(memory_ledger)

    # Act
    caught = pytest.raises(RuntimeError, match="already final at http/502")

    # Assert
    with caught:
        open_turn_exchange(
            agent="scitex-hub",
            probe_url="/v1/exchanges",
            delivery_id="delivery-final",
            _store_factory=memory_ledger.factory,
        )


def test_final_failure_cannot_be_rewritten() -> None:
    # Arrange
    memory_ledger = _MemoryLedger()
    exchange_id, opened_at = _final_failure(memory_ledger)

    # Act
    caught = pytest.raises(RuntimeError, match="refusing to rewrite")

    # Assert
    with caught:
        finish_turn_exchange(
            exchange_id,
            agent="scitex-hub",
            opened_at=opened_at,
            status=StatusCode(kind="http", code=200, message="visible in Hermes"),
            _store_factory=memory_ledger.factory,
        )
