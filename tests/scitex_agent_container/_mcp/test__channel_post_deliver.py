"""Tests for the post-delivery receipts dispatcher
(``_mcp._channel_post_deliver``).

The dispatcher composes the two receipt side-effects (legacy
contentless auto-ack + structural reaction-ack) and is the single
call site channel.py uses after every successful delivery. These
tests pin:

* the missing-config short-circuit (no agent_name / listen_url → no
  side-effects, no crashes — the wake-only path takes this branch);
* both receipts disabled together → early exit (no httpx import on
  the hot path);
* one POST when only the reaction-ack is allowed (the legacy
  auto-ack is empty-content and gets filtered at the sender).

Real ``_FakeListenServer``; no mocks / no monkeypatch on production.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")

from scitex_agent_container._mcp._channel_post_deliver import (  # noqa: E402
    run_post_deliver_receipts,
)
from tests.scitex_agent_container._mcp.test__channel_tools import (  # noqa: E402
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


@contextlib.contextmanager
def _env(name: str, value: str | None):
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


@pytest.mark.asyncio
async def test_run_post_deliver_noop_without_agent_name(fake_listen):
    # Arrange — no ``agent_name`` means there is no sender identity to
    # post FROM; the dispatcher must early-exit cleanly.
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await run_post_deliver_receipts(
        event,
        agent_name=None,
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_run_post_deliver_noop_without_listen_url(fake_listen):
    # Arrange — no ``listen_url`` means we don't know WHERE to POST;
    # early-exit cleanly.
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await run_post_deliver_receipts(
        event,
        agent_name="alice",
        listen_url=None,
        bearer=None,
    )
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_run_post_deliver_noop_when_both_receipts_disabled(fake_listen):
    # Arrange — both env knobs off; zero POSTs reach the listen even
    # though config is otherwise valid.
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    with _env("SAC_CHANNEL_AUTO_ACK", "0"), _env("SAC_REACTION_ACK", "0"):
        await run_post_deliver_receipts(
            event,
            agent_name="alice",
            listen_url=fake_listen.base_url,
            bearer=None,
        )
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_run_post_deliver_only_reaction_posts_when_auto_ack_disabled(
    fake_listen,
):
    # Arrange — auto-ack off, reaction-ack default-on. Only the
    # structural 👀 reaches the wire.
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    with _env("SAC_CHANNEL_AUTO_ACK", "0"), _env("SAC_REACTION_ACK", None):
        await run_post_deliver_receipts(
            event,
            agent_name="alice",
            listen_url=fake_listen.base_url,
            bearer=None,
        )
    # Assert — exactly one structural reaction POST landed.
    reaction_posts = [
        payload
        for _path, payload in fake_listen.posts
        if (
            isinstance(payload, dict)
            and payload.get("params", {}).get("metadata", {}).get("kind") == "reaction"
        )
    ]
    assert len(reaction_posts) == 1
