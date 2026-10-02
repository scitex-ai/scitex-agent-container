"""Immutable exchange binding and explicit authored ACK contract."""

import json
from types import SimpleNamespace

import pytest
from scitex_dev.status import StatusCode
from scitex_dev.store import NEW_RECORD

from scitex_agent_container._state import _agentic_handshake as handshake

TARGET = {
    "agent": "scitex-hub",
    "host": "compute-04",
    "instance_id": "canonical-instance",
    "boot_id": "canonical-instance:123:456",
    "session_id": "native-thread",
}


class MemoryLedger:
    def __init__(self):
        self.values = {}
        self.revisions = {}

    def get(self, key):
        value = self.values.get(key["exchange_id"])
        return None if value is None else SimpleNamespace(values=dict(value))

    def revision(self, key):
        return self.revisions.get(key["exchange_id"])

    def put(self, values, *, expected_revision):
        key = values["exchange_id"]
        if expected_revision is NEW_RECORD:
            assert key not in self.values
        else:
            assert expected_revision == self.revisions.get(key)
        self.values[key] = dict(values)
        self.revisions[key] = self.revisions.get(key, 0) + 1

    def close(self):
        pass


def _feedback(contract, exchange_id):
    return {
        "dispatch_id": contract["nonce"],
        "understood": "Compute and verify session ownership.",
        "owner": TARGET["agent"],
        "next_checkpoint": "Report the current native activity.",
        "handshake_proof": {
            "exchange_id": exchange_id,
            "nonce": contract["nonce"],
            **{key: TARGET[key] for key in ("instance_id", "boot_id", "session_id")},
            "answer": handshake.expected_answer(contract),
        },
    }


def test_server_mints_distinct_nonce_payload_and_exchange_binding_without_expected_answer():
    # Arrange / Act
    first = handshake.make_contract(TARGET, cursor=17, now=100)
    second = handshake.make_contract(TARGET, cursor=17, now=100)
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(first, store_factory=lambda: memory)
    row, contract = handshake.read_handshake(
        exchange_id, agent=TARGET["agent"], store_factory=lambda: memory
    )
    # Assert
    assert first["nonce"] != second["nonce"] and first["payload"] != second["payload"]
    assert (
        contract,
        row["code"],
        row["final"],
        row["initiator"],
        row["responder"],
    ) == (first, 202, False, "sac.listen", TARGET["agent"])
    assert handshake.expected_answer(first) not in handshake.challenge_prompt(
        first, exchange_id
    )


@pytest.mark.parametrize(
    "case",
    [
        "wrong_peer",
        "wrong_nonce",
        "wrong_instance",
        "wrong_boot",
        "wrong_thread",
        "wrong_answer",
        "expired",
        "empty",
        "mechanical",
        "nonce_only",
        "wrong_owner",
    ],
)
def test_ack_requires_exact_fresh_binding_and_authored_fields(case):
    # Arrange
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    feedback = _feedback(contract, "exchange")
    peer, nonce, now = TARGET["agent"], contract["nonce"], 101
    if case == "wrong_peer":
        peer = "another-manager"
    elif case == "wrong_nonce":
        nonce = "stale-nonce"
    elif case.startswith("wrong_") and case[6:] in {
        "instance",
        "boot",
        "thread",
        "answer",
    }:
        key = {
            "instance": "instance_id",
            "boot": "boot_id",
            "thread": "session_id",
            "answer": "answer",
        }[case[6:]]
        feedback["handshake_proof"][key] = "foreign-value"
    elif case == "expired":
        now = 221
    elif case == "empty":
        feedback["understood"] = ""
    elif case == "mechanical":
        feedback["understood"] = "ACK received"
    elif case == "nonce_only":
        feedback["next_checkpoint"] = contract["nonce"]
    elif case == "wrong_owner":
        feedback["owner"] = "daemon"
    # Act / Assert
    assert (
        handshake.validate_ack(
            contract, from_agent=peer, dispatch_id=nonce, feedback=feedback, now=now
        )
        is False
    )


def test_ack_persists_safe_candidate_and_acceptance_cannot_erase_it(monkeypatch):
    # Arrange
    memory = MemoryLedger()
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    exchange_id = handshake.open_handshake(contract, store_factory=lambda: memory)
    feedback = _feedback(contract, exchange_id)
    authored = []
    monkeypatch.setattr(
        handshake,
        "record_agentic_ack",
        lambda *args, **kwargs: authored.append(kwargs) or kwargs,
    )
    event = {"from_agent": TARGET["agent"], "extra": feedback}
    # Act: ACK can race ahead of publish-count persistence.
    saved = handshake.record_handshake_ack(event, now=101, store_factory=lambda: memory)
    handshake.record_acceptance(
        exchange_id, accepted=True, store_factory=lambda: memory
    )
    values, _ = handshake.read_handshake(exchange_id, store_factory=lambda: memory)
    state = json.loads(values["message"])
    # Assert: ACK remains provisional and prose is kept only in dispatch_feedback.
    assert (
        saved
        and state["accepted"] is True
        and state["candidate"]["answer"] == handshake.expected_answer(contract)
    )
    assert values["code"] == 202 and values["final"] is False
    assert feedback["understood"] not in values["message"]
    assert (
        authored[0]["understood"] == feedback["understood"]
        and authored[0]["agent"] == "daemon"
    )


def test_final_exchange_cannot_be_rewritten_as_a_fresh_pending_challenge():
    # Arrange
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    status = StatusCode(kind="http", code=200, message='{"proven":true}')
    handshake.advance_handshake(exchange_id, status, store_factory=lambda: memory)
    # Act / Assert
    with pytest.raises(RuntimeError, match="final handshake"):
        handshake.record_acceptance(
            exchange_id, accepted=True, store_factory=lambda: memory
        )
    assert memory.values[exchange_id]["code"] == 200


@pytest.mark.parametrize(
    "fields",
    [
        ("Verify identity", "Report tests"),
        ("現在のセッションを検証します", "計算結果と進捗を報告します"),
    ],
)
def test_bounded_authored_fields_do_not_require_a_specific_language_or_long_prose(
    fields,
):
    # Arrange: concise meaningful fields still accompany actual independent proof.
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    feedback = _feedback(contract, "exchange")
    feedback["understood"], feedback["next_checkpoint"] = fields
    # Act / Assert
    assert handshake.validate_ack(
        contract,
        from_agent=TARGET["agent"],
        dispatch_id=contract["nonce"],
        feedback=feedback,
        now=101,
    )


def test_foreign_exchange_operation_or_responder_is_not_a_server_challenge():
    # Arrange
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    # Act / Assert
    with pytest.raises(PermissionError):
        handshake.read_handshake(
            exchange_id, agent="foreign-manager", store_factory=lambda: memory
        )
    memory.values[exchange_id]["initiator"] = "another-source"
    with pytest.raises(PermissionError):
        handshake.read_handshake(exchange_id, store_factory=lambda: memory)
