"""Scoped signed acceptance receipts over the real isolated listener HTTP fixture."""

import json
import uuid

import pytest

from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV
from scitex_agent_container._django._exchange import accepted_receipt

EXCHANGE = "xch_20261102T000000Z_gui_fixture_abcdef"


def configure(env, identity="alice", operator=True):
    env.set(IDENTITY_ENV, identity)
    env.set(OPERATORS_ENV, identity if operator else "")


def lookup(client, *, name="alpha", exchange=EXCHANGE, identity="alice", token=None):
    receipt = accepted_receipt(identity, name, exchange) if token is None else token
    return client.get(
        f"/{name}/exchanges/{exchange}", HTTP_X_SAC_EXCHANGE_RECEIPT=receipt
    )


@pytest.mark.parametrize(
    "code,state,final,message",
    [
        (202, "pending", False, "accepted; poll `/v1/exchanges/" + EXCHANGE + "`"),
        (
            102,
            "pending",
            False,
            "native admission is pending; poll `/v1/exchanges/" + EXCHANGE + "`",
        ),
        (200, "received", True, "the target TUI accepted the identified turn"),
        (403, "failed", True, "native admission was refused"),
        (500, "failed", True, "native admission failed"),
    ],
)
def test_native_status_is_admission_not_task_completion(
    client, native_exchange, env_save_restore, code, state, final, message
):
    # Arrange
    configure(env_save_restore)
    native_exchange.exchange_code = code
    native_exchange.exchange_message = message
    # Act
    response = lookup(client)
    data = response.json()
    # Assert
    assert (
        response.status_code,
        data["exchange_id"],
        data["state"],
        data["final"],
        data["code"],
    ) == (200, EXCHANGE, state, final, code)


@pytest.mark.parametrize(
    "name,exchange,identity,suffix,operator",
    [
        ("alpha", EXCHANGE, "alice", "tampered", True),
        ("beta", EXCHANGE, "alice", "", True),
        ("alpha", "xch_20261102T000000Z_other_fixture_abcdef", "alice", "", True),
        ("alpha", EXCHANGE, "bob", "", True),
        ("alpha", EXCHANGE, "alice", "", False),
    ],
)
def test_acceptance_binding_and_control_scope_refuse_before_exchange_lookup(
    client,
    native_exchange,
    env_save_restore,
    name,
    exchange,
    identity,
    suffix,
    operator,
):
    # Arrange
    configure(env_save_restore, identity=identity, operator=operator)
    token = accepted_receipt("alice", "alpha", EXCHANGE) + suffix
    # Act
    response = lookup(client, name=name, exchange=exchange, token=token)
    paths = [p for p in native_exchange.request_paths if "/exchanges/" in p]
    # Assert
    assert (response.status_code, paths) == (403, [])


def test_wrong_native_exchange_cannot_complete_accepted_receipt(
    client, native_exchange, env_save_restore
):
    # Arrange
    configure(env_save_restore)
    native_exchange.exchange_wrong_id = True
    # Act
    response = lookup(client)
    # Assert
    assert (response.status_code, response.json()["state"]) == (502, "unknown")


def test_native_messages_are_redacted_bounded_and_no_payload_is_forwarded(
    client, native_exchange, env_save_restore
):
    # Arrange
    configure(env_save_restore)
    native_exchange.exchange_code = 500
    native_exchange.exchange_message = (
        "failed with token sk-abc123DEF456GHI789jkl012 " + ("x" * 400)
    )
    # Act
    response = lookup(client)
    data = response.json()
    # Assert
    assert (
        "sk-abc123" in data["native_message"],
        len(data["native_message"]) <= 240,
        set(data),
    ) == (
        False,
        True,
        {"exchange_id", "state", "final", "code", "message", "native_message"},
    )


def test_post_acceptance_returns_caller_bound_lookup_receipt(
    client, loopback, env_save_restore, tmp_path
):
    # Arrange
    from scitex_agent_container._django._constants import AUDIT_LOG_ENV

    configure(env_save_restore)
    audit = tmp_path / "message-audit.jsonl"
    env_save_restore.set(AUDIT_LOG_ENV, str(audit))
    # Act
    response = client.post(
        "/alpha/message",
        {
            "message": "private fixture only",
            "dispatch_id": "fixture-" + uuid.uuid4().hex,
        },
        HTTP_ACCEPT="application/json",
    )
    data = response.json()
    result = client.get(
        "/alpha/exchanges/" + EXCHANGE,
        HTTP_X_SAC_EXCHANGE_RECEIPT=data.get("exchange_receipt", ""),
    )
    # Assert
    assert (
        response.status_code,
        data["state"],
        data["exchange_id"],
        result.status_code,
        json.loads(audit.read_text().splitlines()[-1])["exchange_id"],
        "private fixture only" in audit.read_text(),
    ) == (200, "accepted", EXCHANGE, 200, EXCHANGE, False)


@pytest.mark.parametrize(
    "method,exchange,suffix",
    [
        ("GET", EXCHANGE, ""),
        ("GET", EXCHANGE, "bad"),
        ("GET", "invalid", ""),
        ("POST", EXCHANGE, ""),
    ],
)
def test_exchange_receipt_responses_are_private_and_never_cached(
    client, native_exchange, env_save_restore, method, exchange, suffix
):
    # Arrange
    configure(env_save_restore)
    native_exchange.exchange_code = 200
    native_exchange.exchange_message = "native turn admission confirmed"
    token = accepted_receipt("alice", "alpha", EXCHANGE)
    # Act
    response = client.generic(
        method,
        "/alpha/exchanges/" + exchange,
        HTTP_X_SAC_EXCHANGE_RECEIPT=token + suffix,
    )
    cache = response.headers.get("Cache-Control", "")
    # Assert
    assert ("no-store" in cache, "private" in cache) == (True, True)


@pytest.mark.parametrize("name", ["gamma", "absent"])
def test_signed_receipt_does_not_authorize_cross_host_or_missing_recipient(
    client, native_exchange, env_save_restore, name
):
    # Arrange: cross-host visibility does not grant the same-host message route.
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    configure(env_save_restore)
    env_save_restore.set(CROSSHOST_OPERATORS_ENV, "alice")
    token = accepted_receipt("alice", name, EXCHANGE)
    # Act
    response = lookup(client, name=name, token=token)
    paths = [p for p in native_exchange.request_paths if "/exchanges/" in p]
    # Assert
    assert (response.status_code, paths) == (403, [])
