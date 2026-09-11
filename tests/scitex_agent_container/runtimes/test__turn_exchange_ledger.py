"""The TUI worker consumes scitex_dev.status's canonical exchange ledger."""

from __future__ import annotations

from scitex_dev.status import StatusCode, ledger_record, new_exchange_id

from scitex_agent_container.runtimes._turn_exchange_ledger import (
    _store,
    finish_turn_exchange,
    open_turn_exchange,
    read_turn_exchange,
)


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
                    kind="http", code=202, message="accepted; poll the exchange"
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
