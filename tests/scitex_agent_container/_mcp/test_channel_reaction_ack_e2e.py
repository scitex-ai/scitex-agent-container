"""End-to-end tests for the structural reaction-ack receive-side hook
(``feat/comm-reaction-ack``, lead a2a 1781e82a, 2026-06-14).

Two flows:

1. **Emit on inbound** — when ``_push_channel_event`` injects a
   normal inbound event, it ALSO posts a structural ``kind=reaction``
   envelope back to the sender (so the SENDER can detect comm-miss
   via absence). Verified end-to-end against the real
   ``_FakeListenServer`` from ``test_channel.py`` (asyncio TCP +
   real httpx — no mocks).

2. **Absorb on inbound** — when a ``kind=reaction`` event arrives
   at THIS agent (we previously sent a message; the receiver now
   reacts), ``_push_channel_event`` updates the dispatch ledger and
   skips session injection (receipts are not user-visible
   messages). The receive-side adapter does NOT re-react to a
   reaction (no 👀-on-👀 loop).

Conventions: AAA markers, one assert per test (STX-TQ007); no mocks
/ no monkeypatch on production internals.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")

from scitex_agent_container._mcp import channel as channel_mod  # noqa: E402
from scitex_agent_container._mcp.channel import _recent  # noqa: E402

# Reuse the test_channel FakeListen (real SSE + JSON over asyncio TCP).
from tests.scitex_agent_container._mcp.test_channel import (  # noqa: E402
    _CapturingSession,
    _FakeListenServer,
)


@pytest_asyncio.fixture
async def fake_listen():
    server = _FakeListenServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(autouse=True)
def _clear_recent_ring():
    _recent.clear()
    yield
    _recent.clear()


@contextlib.contextmanager
def _env(name: str, value: str | None):
    """Real os.environ save/restore (no monkeypatch on production)."""
    sentinel = object()
    prior: Any = os.environ.get(name, sentinel)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if prior is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db for dispatch-ledger writes during absorption."""
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


# ---------------------------------------------------------------------------
# (1) Emit on inbound normal message — the SENDER's wire signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_message_triggers_reaction_post(fake_listen):
    """A normal inbound message from bob makes alice post a 👀 back."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    paths = [p for p, _ in fake_listen.posts]
    # Assert — at least one POST landed at bob's inbox path
    # (the structural reaction; the legacy contentless auto-ack is
    # suppressed by the empty-ack noise filter, so this POST IS
    # the structural reaction).
    assert "/agents/bob/message:send" in paths


@pytest.mark.asyncio
async def test_reaction_post_body_carries_eyes_marker(fake_listen):
    """The reaction's content is the non-empty eyes marker — that's
    what survives the sender-side empty-ack filter."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Find the reaction POST (kind=reaction in metadata).
    reaction_posts = [
        payload
        for _path, payload in fake_listen.posts
        if (
            isinstance(payload, dict)
            and payload.get("params", {}).get("metadata", {}).get("kind") == "reaction"
        )
    ]
    text = reaction_posts[0]["params"]["message"]["parts"][0]["text"]
    # Assert
    assert text == "\N{EYES}"


@pytest.mark.asyncio
async def test_reaction_post_threads_dispatch_id(fake_listen):
    """The sender minted a ``dispatch_id`` on the original; the
    reaction must thread it back so the sender's adapter can pin the
    exact ledger row."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {
        "from_agent": "bob",
        "content": "hi",
        "msg_id": "m1",
        "dispatch_id": "did-abc",
    }
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    reaction_posts = [
        payload
        for _path, payload in fake_listen.posts
        if (
            isinstance(payload, dict)
            and payload.get("params", {}).get("metadata", {}).get("kind") == "reaction"
        )
    ]
    extra = reaction_posts[0]["params"]["metadata"].get("extra") or {}
    # Assert
    assert extra.get("reacted_dispatch_id") == "did-abc"


@pytest.mark.asyncio
async def test_reaction_post_skipped_when_env_disables_reaction_ack(fake_listen):
    """``SAC_REACTION_ACK=0`` turns the structural receipt off; no
    reaction POSTs reach the listen (verifies the env gate is wired)."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    with _env("SAC_REACTION_ACK", "0"):
        await _push_channel_event(
            session,
            event,
            agent_name="alice",
            listen_url=fake_listen.base_url,
            bearer=None,
        )
    reaction_paths = [
        path
        for path, payload in fake_listen.posts
        if isinstance(payload, dict)
        and payload.get("params", {}).get("metadata", {}).get("kind") == "reaction"
    ]
    # Assert
    assert reaction_paths == []


# ---------------------------------------------------------------------------
# (2) Absorb on inbound reaction — the SENDER's side.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_reaction_skips_session_injection(fake_listen):
    """A ``kind=reaction`` event MUST NOT land as a notifications/claude/
    channel push — it's a wire signal, not a user-visible message."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "content": "\N{EYES}",
        "extra": {"reacted_dispatch_id": "did-x"},
    }
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — the session received NO notification for the reaction.
    assert session.sent == []


@pytest.mark.asyncio
async def test_inbound_reaction_does_not_post_reaction_back(fake_listen):
    """Loop-guard: ``_push_channel_event`` must not 👀-back a 👀.
    Zero POSTs are made when an inbound event is a reaction."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "content": "\N{EYES}",
        "extra": {"reacted_dispatch_id": "did-x"},
    }
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_inbound_reaction_updates_dispatch_ledger(fake_listen, db_path: Path):
    """End-to-end: alice previously sent a message, bob's adapter posts
    a structural reaction back; alice's adapter absorbs it and the
    ledger row flips to REACTED."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    session = _CapturingSession()
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "content": "\N{EYES}",
        "extra": {"reacted_dispatch_id": did},
    }
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_REACTED


@pytest.mark.asyncio
async def test_inbound_reaction_still_lands_in_recent_ring(fake_listen):
    """Even though the reaction is suppressed from session injection,
    it is buffered into ``_recent`` so a2a_inbox callers can AUDIT
    that the receipt landed (debug surface)."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {
        "from_agent": "bob",
        "kind": "reaction",
        "content": "\N{EYES}",
        "msg_id": "react-1",
    }
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    buffered = [e for e in _recent if e.get("msg_id") == "react-1"]
    # Assert
    assert len(buffered) == 1
