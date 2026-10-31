"""Canonical ledger rows cross one passive scoped Store query boundary."""

import json
from types import SimpleNamespace

import pytest
from scitex_dev.status import StatusCode, ledger_record, new_exchange_id
from scitex_dev.store import Op

from scitex_agent_container._listen._handshake_snapshot import (
    handshake_snapshot,
    read_handshake_page,
)
from scitex_agent_container._listen._observation_contract import (
    LedgerPage,
    ObservationAuthority,
)
from scitex_agent_container._state._agentic_handshake import make_contract
from scitex_agent_container.runtimes._activity_source_identity import (
    SOURCE_KIND,
    activity_source_id,
)


@pytest.fixture
def authority():
    target = {
        "agent": "worker",
        "host": "host",
        "instance_id": "instance",
        "boot_id": "boot",
        "session_id": "session",
    }
    return ObservationAuthority(
        **target,
        source={"kind": SOURCE_KIND, "identity": activity_source_id(target, (1, 2))},
    )


def _record(authority, *, issued=100, code=200, verified=104, completed=102):
    target = authority.model_dump(exclude={"source"})
    contract = make_contract(target, cursor=1, now=issued, timeout_s=10)
    contract["source_identity"] = [1, 2]
    state = {
        "proven": True if code == 200 else None,
        "verified_at": verified if code == 200 else None,
        "tool_proof": {
            "completed_at": completed,
            "output": "PRIVATE_NATIVE_OUTPUT",
            "path": "/private/source",
        },
    }
    return ledger_record(
        exchange_id=new_exchange_id(host="host"),
        initiator="sac.listen",
        responder=authority.agent,
        operation=json.dumps(contract),
        opened_at=f"fixture-{issued}",
        status=StatusCode(kind="http", code=code, message=json.dumps(state)),
    )


class _ReadOnlyLedger:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.closed = False

    def search(self, query):
        self.calls.append(query)
        return [SimpleNamespace(values=row) for row in self.records[: query.limit]]

    def close(self):
        self.closed = True


def test_one_read_is_responder_scoped_capped_and_closed(authority):
    # Arrange
    store = _ReadOnlyLedger([_record(authority)])
    # Act
    records, page = read_handshake_page(
        ["worker"], store_factory=lambda: store, limit=2
    )
    query = store.calls[0]
    # Assert
    assert (
        len(store.calls),
        [(p.field, p.op, p.value) for p in query.predicates],
        query.limit,
        query.order[0].field,
        query.order[0].descending,
        len(records),
        page.model_dump(),
        store.closed,
    ) == (
        1,
        [("initiator", Op.EQ, "sac.listen"), ("responder", Op.IN, ("worker",))],
        3,
        "opened_at",
        True,
        1,
        {"limit": 2, "returned": 1, "complete": True},
        True,
    )


def test_page_sentinel_makes_truncated_history_unknown(authority):
    # Arrange
    store = _ReadOnlyLedger([_record(authority, issued=100 + i) for i in range(3)])
    # Act
    records, page = read_handshake_page(
        ["worker"], store_factory=lambda: store, limit=2
    )
    result = handshake_snapshot(authority, records, page, now=106)
    # Assert
    assert (
        result.state,
        result.reason,
        result.last_verified_reply,
        len(records),
        len(store.calls),
    ) == ("unknown", "history_incomplete", None, 2, 1)


def test_later_pending_preserves_original_verified_reply(authority):
    # Arrange
    old = _record(authority)
    pending = _record(authority, issued=105, code=202)
    # Act
    result = handshake_snapshot(
        authority,
        [pending, old],
        LedgerPage(limit=2, returned=2, complete=True),
        now=106,
    )
    # Assert
    assert (
        result.current_exchange.phase,
        result.current_exchange.verified_at,
        result.last_verified_reply.verified_at,
        result.last_verified_reply.verified_age_s,
    ) == ("pending", None, 104, 2)


def test_expired_receipt_is_history_without_current_admission(authority):
    # Arrange
    row = _record(authority)
    # Act
    result = handshake_snapshot(
        authority, [row], LedgerPage(limit=2, returned=1, complete=True), now=112
    )
    # Assert
    assert (
        result.current_exchange.phase,
        result.current_exchange.proven,
        result.last_verified_reply.verified_at,
        result.last_verified_reply.verified_age_s,
    ) == ("expired", None, 104, 8)


@pytest.mark.parametrize(
    "key", ["agent", "host", "instance_id", "boot_id", "session_id"]
)
def test_foreign_authority_cannot_supply_reply_history(authority, key):
    # Arrange
    row = _record(authority)
    operation = json.loads(row["operation"])
    operation["target"][key] = "foreign"
    row["operation"] = json.dumps(operation)
    # Act
    result = handshake_snapshot(
        authority, [row], LedgerPage(limit=2, returned=1, complete=True), now=106
    )
    # Assert
    assert (result.current_exchange, result.last_verified_reply) == (None, None)


def test_replaced_source_cannot_supply_reply_history(authority):
    # Arrange
    row = _record(authority)
    operation = json.loads(row["operation"])
    operation["source_identity"][1] = 3
    row["operation"] = json.dumps(operation)
    # Act
    result = handshake_snapshot(
        authority, [row], LedgerPage(limit=2, returned=1, complete=True), now=106
    )
    # Assert
    assert (result.current_exchange, result.last_verified_reply) == (None, None)


def test_equal_issue_order_is_unknown_instead_of_selecting_by_id(authority):
    # Arrange
    rows = [_record(authority), _record(authority, code=202)]
    # Act
    result = handshake_snapshot(
        authority, rows, LedgerPage(limit=2, returned=2, complete=True), now=106
    )
    # Assert
    assert (result.state, result.reason, result.current_exchange) == (
        "unknown",
        "ambiguous_order",
        None,
    )


@pytest.mark.parametrize("stamp", [None, True, float("nan"), float("inf"), 107, -1])
def test_invalid_verification_clock_never_becomes_a_success(authority, stamp):
    # Arrange
    row = _record(authority, verified=stamp)
    # Act
    result = handshake_snapshot(
        authority, [row], LedgerPage(limit=2, returned=1, complete=True), now=106
    )
    # Assert
    assert (result.last_verified_reply, result.current_exchange.verified_age_s) == (
        None,
        None,
    )


def test_raw_proof_and_private_text_never_cross_snapshot(authority):
    # Arrange
    row = _record(authority)
    # Act
    result = handshake_snapshot(
        authority, [row], LedgerPage(limit=2, returned=1, complete=True), now=106
    ).model_dump_json()
    # Assert
    assert not any(
        value in result
        for value in (
            "PRIVATE_NATIVE_OUTPUT",
            "/private/source",
            "tool_proof",
            "nonce",
            "payload",
        )
    )


@pytest.mark.parametrize("issued", [True, float("nan"), float("inf"), 107, -1])
def test_bad_own_exchange_clock_cannot_restore_an_older_current_frame(
    authority, issued
):
    # Arrange
    old = _record(authority)
    newer = _record(authority, issued=105, code=202)
    operation = json.loads(newer["operation"])
    operation["issued_at"] = issued
    newer["operation"] = json.dumps(operation)
    # Act
    result = handshake_snapshot(
        authority, [newer, old], LedgerPage(limit=2, returned=2, complete=True), now=106
    )
    # Assert
    assert (result.state, result.reason, result.current_exchange) == (
        "unknown",
        "clock_uncertain",
        None,
    )
