"""Immutable exchange binding and explicit authored ACK contract."""

import json
from functools import partial
from types import SimpleNamespace

import pytest
from scitex_dev.status import StatusCode
from scitex_dev.store import NEW_RECORD

from scitex_agent_container._state import _agentic_handshake as handshake
from scitex_agent_container._state.dispatch_feedback import record_agentic_ack

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


def test_server_mints_distinct_nonce_and_payload():
    # Arrange
    # Act
    first = handshake.make_contract(TARGET, cursor=17, now=100)
    second = handshake.make_contract(TARGET, cursor=17, now=100)
    # Assert
    assert first["nonce"] != second["nonce"] and first["payload"] != second["payload"]


def test_exchange_binds_the_original_pending_server_contract():
    # Arrange
    first = handshake.make_contract(TARGET, cursor=17, now=100)
    memory = MemoryLedger()
    # Act
    exchange_id = handshake.open_handshake(first, store_factory=lambda: memory)
    row, contract = handshake.read_handshake(
        exchange_id, agent=TARGET["agent"], store_factory=lambda: memory
    )
    # Assert
    assert (
        contract,
        row["code"],
        row["final"],
        row["initiator"],
        row["responder"],
    ) == (first, 202, False, "sac.listen", TARGET["agent"])


def test_server_challenge_does_not_supply_the_computed_answer():
    # Arrange
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    # Act
    prompt = handshake.challenge_prompt(contract, "exchange")
    # Assert
    assert handshake.expected_answer(contract) not in prompt


def _replace_proof(key):
    return lambda args: args["feedback"]["handshake_proof"].update(
        {key: "foreign-value"}
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda args: args.update(from_agent="another-manager"), id="wrong_peer"
        ),
        pytest.param(
            lambda args: args.update(dispatch_id="stale-nonce"), id="wrong_nonce"
        ),
        pytest.param(_replace_proof("instance_id"), id="wrong_instance"),
        pytest.param(_replace_proof("boot_id"), id="wrong_boot"),
        pytest.param(_replace_proof("session_id"), id="wrong_thread"),
        pytest.param(_replace_proof("answer"), id="wrong_answer"),
        pytest.param(lambda args: args.update(now=221), id="expired"),
        pytest.param(lambda args: args["feedback"].update(understood=""), id="empty"),
        pytest.param(
            lambda args: args["feedback"].update(understood="ACK received"),
            id="mechanical",
        ),
        pytest.param(
            lambda args: args["feedback"].update(next_checkpoint=args["dispatch_id"]),
            id="nonce_only",
        ),
        pytest.param(
            lambda args: args["feedback"].update(owner="daemon"), id="wrong_owner"
        ),
    ],
)
def test_ack_requires_exact_fresh_binding_and_authored_fields(mutate):
    # Arrange
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    feedback = _feedback(contract, "exchange")
    args = dict(
        from_agent=TARGET["agent"],
        dispatch_id=contract["nonce"],
        feedback=feedback,
        now=101,
    )
    mutate(args)
    # Act
    # Assert
    assert handshake.validate_ack(contract, **args) is False


@pytest.fixture
def accepted_candidate():
    # Arrange
    memory = MemoryLedger()
    contract = handshake.make_contract(TARGET, cursor=17, now=100)
    exchange_id = handshake.open_handshake(contract, store_factory=lambda: memory)
    feedback = _feedback(contract, exchange_id)
    dispatch = {"agent": "daemon", "to_agent": TARGET["agent"], "status": "sent"}
    authored = {}
    recorder = partial(
        record_agentic_ack,
        dispatch_lookup=lambda did, **kw: dispatch,
        feedback_lookup=lambda did, agent: authored.get(did),
        feedback_write=lambda values, **kw: authored.setdefault(
            values["dispatch_id"], dict(values)
        ),
        dispatch_update=lambda did, status, **kw: dispatch.update(status=status),
    )
    event = {"from_agent": TARGET["agent"], "extra": feedback}
    # Act: ACK can race ahead of publish-count persistence.
    saved = handshake.record_handshake_ack(
        event, now=101, store_factory=lambda: memory, record_ack=recorder
    )
    handshake.record_acceptance(
        exchange_id, accepted=True, store_factory=lambda: memory
    )
    values, _ = handshake.read_handshake(exchange_id, store_factory=lambda: memory)
    state = json.loads(values["message"])
    return {
        "candidate_retained": (
            saved
            and state["accepted"] is True
            and state["candidate"]["answer"] == handshake.expected_answer(contract)
        ),
        "pending": values["code"] == 202 and values["final"] is False,
        "safe_ledger": feedback["understood"] not in values["message"],
        "authored_store": authored[contract["nonce"]]["understood"]
        == feedback["understood"]
        and authored[contract["nonce"]]["agent"] == "daemon",
    }


@pytest.mark.parametrize(
    "key", ["candidate_retained", "pending", "safe_ledger", "authored_store"]
)
def test_ack_persists_safe_candidate_and_acceptance_cannot_erase_it(
    accepted_candidate, key
):
    # Arrange
    observations = accepted_candidate
    # Act
    value = observations[key]
    # Assert
    assert value is True


def test_final_exchange_cannot_be_rewritten_as_a_fresh_pending_challenge():
    # Arrange
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    status = StatusCode(kind="http", code=200, message='{"proven":true}')
    handshake.advance_handshake(exchange_id, status, store_factory=lambda: memory)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="final handshake"):
        handshake.record_acceptance(
            exchange_id, accepted=True, store_factory=lambda: memory
        )


@pytest.fixture
def refused_final_update():
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    handshake.advance_handshake(
        exchange_id,
        StatusCode(kind="http", code=200, message='{"proven":true}'),
        store_factory=lambda: memory,
    )
    with pytest.raises(RuntimeError, match="final handshake"):
        handshake.record_acceptance(
            exchange_id, accepted=True, store_factory=lambda: memory
        )
    return memory.values[exchange_id]


def test_refused_acceptance_preserves_the_final_exchange(refused_final_update):
    # Arrange
    row = refused_final_update
    # Act
    code = row["code"]
    # Assert
    assert code == 200


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
    # Act
    # Assert
    assert handshake.validate_ack(
        contract,
        from_agent=TARGET["agent"],
        dispatch_id=contract["nonce"],
        feedback=feedback,
        now=101,
    )


def test_foreign_responder_is_not_this_server_challenge():
    # Arrange
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    # Act
    # Assert
    with pytest.raises(PermissionError):
        handshake.read_handshake(
            exchange_id, agent="foreign-manager", store_factory=lambda: memory
        )


def test_foreign_exchange_initiator_is_not_a_server_challenge():
    # Arrange
    memory = MemoryLedger()
    exchange_id = handshake.open_handshake(
        handshake.make_contract(TARGET, cursor=0, now=100), store_factory=lambda: memory
    )
    memory.values[exchange_id]["initiator"] = "another-source"
    # Act
    # Assert
    with pytest.raises(PermissionError):
        handshake.read_handshake(exchange_id, store_factory=lambda: memory)
