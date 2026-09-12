"""The TUI worker consumes scitex_dev.status's canonical exchange ledger."""

from __future__ import annotations

from scitex_dev.status import StatusCode, ledger_record, new_exchange_id

from scitex_agent_container.runtimes._turn_exchange_ledger import (
    _access_error,
    _privilege_failure,
    _store,
    finish_turn_exchange,
    open_turn_exchange,
    read_turn_exchange,
)


class _PrivilegeDenied(Exception):
    sqlstate = "42501"


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
