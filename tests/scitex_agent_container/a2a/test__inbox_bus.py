"""Tests for the per-agent inbox pub/sub broker (commit 1 of the
A2A push channel slice). See docs/sac-and-orochi.md.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.a2a._inbox_bus import Broker, mint_event


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_a_noop() -> None:
    broker = Broker()
    delivered = await broker.publish("nobody", {"x": 1})
    assert delivered == 0


@pytest.mark.asyncio
async def test_single_subscriber_receives_published_event() -> None:
    broker = Broker()
    q = await broker.subscribe("alice")
    delivered = await broker.publish("alice", {"x": 1})
    assert delivered == 1
    assert q.get_nowait() == {"x": 1}


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_subscriber() -> None:
    broker = Broker()
    q1 = await broker.subscribe("alice")
    q2 = await broker.subscribe("alice")
    delivered = await broker.publish("alice", {"x": 2})
    assert delivered == 2
    assert q1.get_nowait() == {"x": 2}
    assert q2.get_nowait() == {"x": 2}


@pytest.mark.asyncio
async def test_publish_does_not_cross_agent_boundaries() -> None:
    broker = Broker()
    alice_q = await broker.subscribe("alice")
    bob_q = await broker.subscribe("bob")
    await broker.publish("alice", {"x": "for-alice"})
    assert alice_q.get_nowait() == {"x": "for-alice"}
    assert bob_q.empty()


@pytest.mark.asyncio
async def test_unsubscribe_removes_queue() -> None:
    broker = Broker()
    q = await broker.subscribe("alice")
    await broker.unsubscribe("alice", q)
    delivered = await broker.publish("alice", {"x": 3})
    assert delivered == 0


@pytest.mark.asyncio
async def test_slow_consumer_drops_oldest_not_blocks_publisher() -> None:
    """Bounded queue protects publisher latency. The first event lands;
    once the cap (64) is exceeded, the oldest entry is evicted so the
    newest still fits."""
    broker = Broker()
    q = await broker.subscribe("alice")
    for i in range(70):
        await broker.publish("alice", {"i": i})
    # Queue cap is 64 — newest 64 should be present, oldest dropped.
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert len(drained) == 64
    indices = [d["i"] for d in drained]
    assert indices[-1] == 69
    assert indices[0] >= 6  # oldest few dropped


def test_mint_event_shape() -> None:
    e = mint_event(
        "alice",
        content="hello",
        from_agent="bob",
        conversation_id="c1",
        in_reply_to="m0",
        priority="high",
        requires_reply=True,
    )
    assert e["to_agent"] == "alice"
    assert e["from_agent"] == "bob"
    assert e["content"] == "hello"
    assert e["conversation_id"] == "c1"
    assert e["in_reply_to"] == "m0"
    assert e["priority"] == "high"
    assert e["requires_reply"] is True
    assert isinstance(e["msg_id"], str) and len(e["msg_id"]) >= 16
    assert isinstance(e["ts"], float)


def test_mint_event_defaults_safe() -> None:
    e = mint_event("alice", content="hi")
    assert e["from_agent"] == "unknown"
    assert e["priority"] == "normal"
    assert e["requires_reply"] is False
    assert "in_reply_to" not in e
    assert "conversation_id" not in e
