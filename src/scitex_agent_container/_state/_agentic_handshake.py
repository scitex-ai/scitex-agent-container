"""Server-origin challenge state in the existing protocol exchange ledger."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone

from scitex_dev.status import StatusCode, is_exchange_id, ledger_record, new_exchange_id

from ..runtimes._turn_exchange_ledger import _store
from .dispatch_feedback import record_agentic_ack

OPERATION = "sac.agentic-handshake/v1"
DAEMON = "daemon"
_TARGET_KEYS = {"agent", "host", "instance_id", "boot_id", "session_id"}


def expected_answer(contract: dict) -> str:
    """Compute independently; this digest is not included in the challenge."""
    return hashlib.sha256(
        f"{contract['nonce']}:{contract['payload']}".encode("ascii")
    ).hexdigest()


def make_contract(target: dict, *, cursor: int, now: float, timeout_s=120.0) -> dict:
    """Mint every correlation value on the server, never from a request body."""
    if set(target) != _TARGET_KEYS or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value)
        for value in target.values()
    ):
        raise ValueError("handshake needs exact canonical target identities")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("handshake needs an observed native activity cursor")
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in (now, timeout_s)
        )
        or timeout_s > 300
    ):
        raise ValueError("handshake needs a finite server deadline of at most 300s")
    return {
        "schema": OPERATION,
        "target": dict(target),
        "cursor": cursor,
        "nonce": secrets.token_hex(16),
        "payload": secrets.token_hex(32),
        "issued_at": float(now),
        "deadline": float(now + timeout_s),
    }


def challenge_prompt(contract: dict, exchange_id: str) -> str:
    """Request bounded authorship and a real computation in this exact thread."""
    target = contract["target"]
    return (
        "SAC listen issued an agentic handshake for your current native session. "
        "Use a tool in this thread to compute and print lowercase SHA256 of the "
        f"ASCII string {contract['nonce']}:{contract['payload']}. "
        "Then call a2a_agentic_ack with the dispatch_id below, your own understood "
        "summary, owner equal to your agent name, and a concrete next_checkpoint. "
        "Include handshake_proof containing exchange_id, nonce, instance_id, "
        "boot_id, session_id, and the computed answer. An echo, subscriber receipt, "
        "or another thread's work does not prove this handshake. "
        f"dispatch_id={contract['nonce']} exchange_id={exchange_id} "
        f"instance_id={target['instance_id']} boot_id={target['boot_id']} "
        f"session_id={target['session_id']}"
    )


def _status(exchange_id, agent, *, candidate=None, accepted=None):
    return StatusCode(
        kind="http",
        code=202,
        message=json.dumps(
            {
                "phase": "awaiting_agentic_proof",
                "accepted": accepted,
                "candidate": candidate,
                "poll": f"`/agents/{agent}/handshakes/{exchange_id}`",
            },
            sort_keys=True,
        ),
    )


def open_handshake(contract: dict, *, store_factory=None) -> str:
    """Use Dev's already provisioned ledger; never provision a new store."""
    from scitex_dev.store import NEW_RECORD

    exchange_id = new_exchange_id(host=contract["target"]["host"])
    store = (store_factory or _store)()
    try:
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator="sac.listen",
                responder=contract["target"]["agent"],
                operation=json.dumps(contract, sort_keys=True, separators=(",", ":")),
                opened_at=datetime.fromtimestamp(
                    contract["issued_at"], timezone.utc
                ).isoformat(),
                status=_status(exchange_id, contract["target"]["agent"]),
            ),
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    return exchange_id


def read_handshake(exchange_id: str, *, agent=None, store_factory=None):
    """Decode only this exact exchange and immutable handshake operation."""
    if not is_exchange_id(exchange_id):
        raise ValueError("handshake exchange_id is not canonical")
    store = (store_factory or _store)()
    try:
        row = store.get({"exchange_id": exchange_id})
        if row is None:
            raise LookupError("handshake exchange not found")
        values = dict(row.values)
    finally:
        store.close()
    try:
        contract = json.loads(values["operation"])
        if (
            contract["schema"] != OPERATION
            or values["initiator"] != "sac.listen"
            or values["responder"] != contract["target"]["agent"]
            or (agent is not None and values["responder"] != agent)
        ):
            raise ValueError("wrong operation or target")
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("exchange is not this target's server handshake") from exc
    return values, contract


def advance_handshake(exchange_id, status=None, *, status_fn=None, store_factory=None):
    """Fence concurrent updates and preserve all immutable exchange fields."""
    store = (store_factory or _store)()
    key = {"exchange_id": exchange_id}
    try:
        revision = store.revision(key)
        row = store.get(key)
        if row is None:
            raise LookupError("handshake exchange disappeared")
        values = dict(row.values)
        if status_fn is not None:
            status = status_fn(values)
        if values["final"]:
            if (values["code"], values["message"]) == (status.code, status.message):
                return
            raise RuntimeError("refusing to rewrite a final handshake exchange")
        store.put(
            ledger_record(
                exchange_id=exchange_id,
                initiator=values["initiator"],
                responder=values["responder"],
                operation=values["operation"],
                opened_at=values["opened_at"],
                status=status,
            ),
            expected_revision=revision,
        )
    finally:
        store.close()


def record_acceptance(exchange_id, *, accepted, store_factory=None):
    _, contract = read_handshake(exchange_id, store_factory=store_factory)
    advance_handshake(
        exchange_id,
        status_fn=lambda values: _status(
            exchange_id,
            contract["target"]["agent"],
            candidate=json.loads(values["message"]).get("candidate"),
            accepted=accepted,
        ),
        store_factory=store_factory,
    )


def validate_ack(contract, *, from_agent, dispatch_id, feedback, now):
    """Reject stale, foreign, empty, mechanical and nonce-only acknowledgements."""
    target = contract["target"]
    proof = feedback.get("handshake_proof")
    if not isinstance(proof, dict) or now > contract["deadline"]:
        return False
    if set(proof) != {
        "exchange_id",
        "nonce",
        "instance_id",
        "boot_id",
        "session_id",
        "answer",
    }:
        return False
    if any(not isinstance(value, str) or not value for value in proof.values()):
        return False
    if from_agent != target["agent"] or dispatch_id != contract["nonce"]:
        return False
    if any(
        proof.get(key) != target[key]
        for key in ("instance_id", "boot_id", "session_id")
    ):
        return False
    if proof.get("nonce") != contract["nonce"] or proof.get(
        "answer"
    ) != expected_answer(contract):
        return False
    if feedback.get("owner") != target["agent"]:
        return False
    for key in ("understood", "next_checkpoint"):
        text = feedback.get(key)
        if not isinstance(text, str) or len(text) > 500:
            return False
        scrubbed = text.casefold()
        for identity in (
            *target.values(),
            contract["nonce"],
            contract["payload"],
            proof["exchange_id"],
            expected_answer(contract),
        ):
            scrubbed = scrubbed.replace(identity.casefold(), "")
        words = re.findall(r"[^\W\d_]{2,}", scrubbed, re.UNICODE)
        mechanical = {
            "ack",
            "okay",
            "received",
            "receipt",
            "accepted",
            "delivered",
            "subscriber",
            "echo",
            "nonce",
            "ping",
            "pong",
            "alive",
            "understood",
            "successfully",
            "ready",
            "yes",
            "ok",
            "the",
            "and",
            "is",
            "for",
            "with",
            "on",
            "了解しました",
            "受信しました",
            "承認済み",
            "確認済み",
            "はい",
        }
        content_words = [word for word in words if word not in mechanical]
        unspaced_language = any(
            len(word) >= 4 and any(not char.isascii() for char in word)
            for word in content_words
        )
        if len(content_words) < 2 and not unspaced_language:
            return False
    return True


def record_handshake_ack(
    event: dict, *, now=None, store_factory=None, record_ack=None
) -> bool:
    """Save authored fields in dispatch_feedback and safe proof IDs/hash only."""
    feedback = event.get("extra", {})
    proof = feedback.get("handshake_proof", {})
    exchange_id = proof.get("exchange_id")
    values, contract = read_handshake(exchange_id, store_factory=store_factory)
    if not validate_ack(
        contract,
        from_agent=event.get("from_agent"),
        dispatch_id=feedback.get("dispatch_id"),
        feedback=feedback,
        now=time.time() if now is None else now,
    ):
        return False
    safe = {
        key: proof[key]
        for key in ("nonce", "instance_id", "boot_id", "session_id", "answer")
    }
    state = json.loads(values["message"])
    if state.get("candidate") not in (None, safe):
        return False
    saved = (record_ack or record_agentic_ack)(
        feedback["dispatch_id"],
        from_agent=event["from_agent"],
        understood=feedback.get("understood"),
        owner=feedback.get("owner"),
        next_checkpoint=feedback.get("next_checkpoint"),
        agent=DAEMON,
    )
    if saved is None:
        return False
    if values["final"]:
        return values["code"] == 200 and state.get("candidate") == safe

    def status_fn(current):
        state = json.loads(current["message"])
        if state.get("candidate") not in (None, safe):
            raise RuntimeError("conflicting handshake proof replay")
        return _status(
            exchange_id,
            contract["target"]["agent"],
            candidate=safe,
            accepted=state.get("accepted"),
        )

    advance_handshake(exchange_id, status_fn=status_fn, store_factory=store_factory)
    return True
