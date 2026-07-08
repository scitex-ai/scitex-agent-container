"""Tests for the sac channel structural reaction-ack subsystem
(``_mcp._channel_reaction_ack``, ``feat/comm-reaction-ack``).

Three surfaces are covered, no mocks / monkeypatch on production:

1. Pure predicates — :func:`should_emit_reaction_ack` and
   :func:`is_reaction_event`. Structural, no I/O.

2. Receive-side emit — :func:`post_reaction_ack` posts a non-empty
   ``kind="reaction"`` envelope back to the sender's listen URL. Uses
   the real ``_FakeListenServer`` from
   ``tests/.../_mcp/test__channel_tools.py`` (asyncio.start_server +
   real httpx — no mocks).

3. Sender-side absorption — :func:`absorb_reaction_ack` walks the
   envelope and flips the dispatch-ledger row to ``STATUS_REACTED``.
   Real sqlite under ``tmp_path`` via the same env-fixture pattern
   the ledger tests use.

Operator mandate (lead a2a ``1781e82a``, 2026-06-14): structural
reaction so absence = comm miss, detectable.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")

from scitex_agent_container._mcp._channel_reaction_ack import (  # noqa: E402
    DEFAULT_REACTION_MARKER,
    absorb_reaction_ack,
    is_reaction_event,
    post_reaction_ack,
    reaction_ack_enabled,
    reaction_ack_marker,
    should_emit_reaction_ack,
)

# Reuse the real HTTP fake from the sibling test module — same shape
# as test__channel_ack_filter does. No mocks.
from tests.scitex_agent_container._mcp.test__channel_tools import (  # noqa: E402
    _FakeListenServer,
)

# ---------------------------------------------------------------------------
# (1) Pure predicates
# ---------------------------------------------------------------------------


def test_is_reaction_event_true_when_kind_is_reaction():
    # Arrange
    event = {"kind": "reaction", "from_agent": "bob"}
    # Act
    decision = is_reaction_event(event)
    # Assert
    assert decision is True


def test_is_reaction_event_false_on_normal_message():
    # Arrange
    event = {"kind": "message", "from_agent": "bob"}
    # Act
    decision = is_reaction_event(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_true_for_normal_message():
    # Arrange
    event = {"from_agent": "bob", "content": "hello"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is True


def test_should_emit_reaction_ack_false_when_no_sender():
    # Arrange — no ``from_agent`` means nowhere to post the receipt.
    event: dict[str, Any] = {"content": "hello"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_false_on_ack_marker():
    # Arrange — a stage-2 auto-ack must not trigger a reaction (would
    # double-stamp the same delivery).
    event = {"from_agent": "bob", "ack": True}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_false_on_reaction_event():
    # Arrange — 👀-on-👀 is the loop we are guarding against.
    event = {"from_agent": "bob", "kind": "reaction", "content": "\N{EYES}"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_false_on_denied_attempt():
    # Arrange — denied_attempt is a structured notification; reacting
    # would post back to the would-be sender and pollute the ledger.
    event = {"from_agent": "carol", "kind": "denied_attempt"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_false_on_system_sender():
    # Arrange — system notifications must never be reacted to (the
    # 'system' sender has no inbox we should write to).
    event = {"from_agent": "system", "kind": "acl_deny_notify"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


def test_should_emit_reaction_ack_false_on_daemon_sender():
    # Arrange — the canonical daemon sender (operator directive
    # 2026-07-05, bracket form) must be treated exactly like the bare
    # 'system' sender: no reaction back to a non-agent sender.
    from scitex_agent_container.a2a._inbox_bus import DAEMON_SENDER

    event = {"from_agent": DAEMON_SENDER, "kind": "acl_deny_notify"}
    # Act
    decision = should_emit_reaction_ack(event)
    # Assert
    assert decision is False


# ---------------------------------------------------------------------------
# (2) Env-driven config
# ---------------------------------------------------------------------------


@pytest.fixture
def env_setter():
    """Save/restore env keys per test — no monkeypatch on production."""
    saved: dict[str, str | None] = {}

    def _set(key: str, value: str | None) -> None:
        if key not in saved:
            saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    try:
        yield _set
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def test_reaction_ack_enabled_default_is_true(env_setter):
    # Arrange — clear env so default applies.
    env_setter("SAC_REACTION_ACK", None)
    # Act
    decision = reaction_ack_enabled()
    # Assert
    assert decision is True


def test_reaction_ack_disabled_when_env_zero(env_setter):
    # Arrange
    env_setter("SAC_REACTION_ACK", "0")
    # Act
    decision = reaction_ack_enabled()
    # Assert
    assert decision is False


def test_reaction_ack_marker_default_is_eyes(env_setter):
    # Arrange — clear env so default applies (both keys).
    env_setter("SAC_REACTION_ACK_MARKER", None)
    env_setter("SCITEX_AGENT_CONTAINER_REACTION_ACK_MARKER", None)
    # Act
    marker = reaction_ack_marker()
    # Assert
    assert marker == DEFAULT_REACTION_MARKER


def test_reaction_ack_marker_empty_override_falls_back(env_setter):
    # Arrange — an empty marker would re-trigger the empty-ack filter;
    # the helper must reject it silently and use the default.
    env_setter("SAC_REACTION_ACK_MARKER", "   ")
    # Act
    marker = reaction_ack_marker()
    # Assert
    assert marker == DEFAULT_REACTION_MARKER


# ---------------------------------------------------------------------------
# (3) Receive-side emit — real HTTP fake.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_listen():
    server = _FakeListenServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_post_reaction_ack_emits_a_post_to_sender(fake_listen):
    # Arrange — alice receives a message from bob; she reacts back.
    event = {"from_agent": "bob", "msg_id": "m1", "content": "hello"}
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    paths = [p for p, _ in fake_listen.posts]
    # Assert
    assert "/agents/bob/message:send" in paths


@pytest.mark.asyncio
async def test_post_reaction_ack_payload_carries_marker_text(fake_listen):
    # Arrange
    event = {"from_agent": "bob", "msg_id": "m1", "content": "hello"}
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    payload = fake_listen.posts[0][1]
    text = payload["params"]["message"]["parts"][0]["text"]
    # Assert — content is the non-empty marker that survives the
    # sender-side empty-ack filter.
    assert text == DEFAULT_REACTION_MARKER


@pytest.mark.asyncio
async def test_post_reaction_ack_payload_carries_kind_reaction(fake_listen):
    # Arrange
    event = {"from_agent": "bob", "msg_id": "m1", "content": "hello"}
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    metadata = fake_listen.posts[0][1]["params"]["metadata"]
    # Assert
    assert metadata.get("kind") == "reaction"


@pytest.mark.asyncio
async def test_post_reaction_ack_threads_dispatch_id_into_extra(fake_listen):
    # Arrange — the inbound event carries the SENDER's dispatch_id so
    # the reaction can pin the exact ledger row.
    event = {
        "from_agent": "bob",
        "msg_id": "m1",
        "content": "hi",
        "dispatch_id": "did-abc",
    }
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    metadata = fake_listen.posts[0][1]["params"]["metadata"]
    extra = metadata.get("extra") or {}
    # Assert
    assert extra.get("reacted_dispatch_id") == "did-abc"


@pytest.mark.asyncio
async def test_post_reaction_ack_marks_envelope_with_ack_loop_guard(fake_listen):
    # Arrange — the legacy auto-ack subsystem refuses to ack an event
    # with ``ack=True``; setting it on the reaction preserves that
    # guard against the auto-ack path re-firing on the receipt.
    event = {"from_agent": "bob", "msg_id": "m1", "content": "hi"}
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    metadata = fake_listen.posts[0][1]["params"]["metadata"]
    # Assert
    assert metadata.get("ack") is True


# ---------------------------------------------------------------------------
# (4) Sender-side absorption — flips the ledger row to REACTED.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange — isolated state.db via env, mirroring the ledger tests.
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def test_absorb_reaction_ack_flips_ledger_to_reacted(db_path: Path):
    # Arrange — record a dispatch, simulate the receiver's 👀 landing.
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "content": DEFAULT_REACTION_MARKER,
        "extra": {"reacted_dispatch_id": did},
    }
    # Act
    absorb_reaction_ack(event)
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_REACTED


def test_absorb_reaction_ack_returns_true_on_match(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "extra": {"reacted_dispatch_id": did},
    }
    # Act
    matched = absorb_reaction_ack(event)
    # Assert
    assert matched is True


def test_absorb_reaction_ack_returns_false_on_non_reaction_event(db_path: Path):
    # Arrange — a regular inbound message should never touch the
    # ledger. Returning False is the audit signal.
    event = {
        "from_agent": "bob",
        "content": "hi",
        "extra": {"reacted_dispatch_id": "did-x"},
    }
    # Act
    matched = absorb_reaction_ack(event)
    # Assert
    assert matched is False


def test_absorb_reaction_ack_returns_false_without_dispatch_id(db_path: Path):
    # Arrange — legacy senders that never minted a ledger row are
    # not a failure; they're a no-op (deliberate, not silent).
    event: dict[str, Any] = {"from_agent": "bob", "kind": "reaction"}
    # Act
    matched = absorb_reaction_ack(event)
    # Assert
    assert matched is False


def test_absorb_reaction_ack_is_idempotent_on_double_delivery(db_path: Path):
    # Arrange — a retried receipt must not corrupt the row.
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "extra": {"reacted_dispatch_id": did},
    }
    absorb_reaction_ack(event)
    # Act — second delivery.
    absorb_reaction_ack(event)
    rows = list_dispatches()
    # Assert — row is still REACTED, no schema corruption, single row.
    assert rows[0]["status"] == STATUS_REACTED


# ---------------------------------------------------------------------------
# (5) Comm-miss detectability — sender notices an absent reaction.
# ---------------------------------------------------------------------------


def test_comm_miss_detected_when_receipt_never_lands(db_path: Path):
    # Arrange — sender records a dispatch but the receiver never
    # reacts (their adapter is down).
    import time

    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    record_dispatch(
        from_agent="alice",
        to_agent="bob",
        text="hi",
        ts=time.time() - 60.0,
    )
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0, from_agent="alice")
    # Assert — the comm miss is visible.
    assert len(rows) == 1


def test_comm_miss_cleared_after_reaction_lands(db_path: Path):
    # Arrange — same dispatch, but the receipt landed via absorption.
    import time

    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    did = record_dispatch(
        from_agent="alice",
        to_agent="bob",
        text="hi",
        ts=time.time() - 60.0,
    )
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "extra": {"reacted_dispatch_id": did},
    }
    absorb_reaction_ack(event)
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0, from_agent="alice")
    # Assert — no miss; the reaction landed.
    assert rows == []


# ---------------------------------------------------------------------------
# (6) Payload shape sanity — the outbound envelope mirrors the
# operator's wire contract (JSON-RPC SendMessage with sac metadata).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_reaction_ack_envelope_is_json_rpc_send_message(fake_listen):
    # Arrange
    event = {"from_agent": "bob", "msg_id": "m1", "content": "hi"}
    # Act
    await post_reaction_ack(
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    payload = fake_listen.posts[0][1]
    serialised = json.dumps(payload)  # round-trips; envelope is JSON.
    # Assert — minimal sanity: the method is SendMessage.
    assert "SendMessage" in serialised
