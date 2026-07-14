"""The inbox bus's idle KEEPALIVE beat — three outcomes, never two.

``Broker.get_or_close`` used to answer a binary question: an event, or ``None``
meaning "closing". An idle stream is neither, and with only two answers the SSE
handler had to park forever waiting for one of them. A stream that never writes
cannot be told apart from a stream that has DIED silently (no FIN, no RST), so:

  * the CLIENT parks on an unbounded read believing it is subscribed;
  * the SERVER keeps the subscriber's queue registered, reports a PHANTOM
    subscriber, and ``a2a_send`` claims a delivery nobody will ever read.

:data:`KEEPALIVE` is the third answer — "idle, and healthy". These tests pin the
contract, and above all pin the property a beat must never violate: **an event
must never be lost to it.**

Real ``asyncio.Queue`` + the real ``Broker``; no mocks.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.a2a._inbox_bus import (
    DEFAULT_KEEPALIVE_INTERVAL_S,
    ENV_KEEPALIVE_INTERVAL_S,
    KEEPALIVE,
    Broker,
    keepalive_interval_s,
    mint_event,
)


@pytest.fixture
def keepalive_env():
    """Set the REAL env var the production code reads, and restore it after.

    The interval is resolved from the environment at CALL time precisely so a
    deployment (or a test) can steer it; this exercises that real path rather
    than rewriting an internal.
    """
    previous = os.environ.get(ENV_KEEPALIVE_INTERVAL_S)

    def _set(value: str) -> None:
        os.environ[ENV_KEEPALIVE_INTERVAL_S] = value

    try:
        yield _set
    finally:
        if previous is None:
            os.environ.pop(ENV_KEEPALIVE_INTERVAL_S, None)
        else:
            os.environ[ENV_KEEPALIVE_INTERVAL_S] = previous


@pytest.fixture
def no_keepalive_env():
    """Guarantee the interval env var is ABSENT, restoring any prior value."""
    previous = os.environ.pop(ENV_KEEPALIVE_INTERVAL_S, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[ENV_KEEPALIVE_INTERVAL_S] = previous


@pytest.mark.asyncio
async def test_idle_stream_returns_keepalive_sentinel():
    # Arrange — a subscriber with nothing to receive.
    broker = Broker()
    queue = await broker.subscribe("alice")
    # Act — wait past the beat interval with no event published.
    result = await broker.get_or_close(queue, keepalive_after=0.05)
    # Assert — idle reports KEEPALIVE, NOT None. Returning None here would tear
    # down a perfectly healthy stream on every interval.
    assert result is KEEPALIVE


@pytest.mark.asyncio
async def test_pending_event_wins_over_keepalive_beat():
    # Arrange — an event is already queued when the beat interval elapses.
    broker = Broker()
    queue = await broker.subscribe("alice")
    await broker.publish("alice", mint_event("alice", content="hi"))
    # Act
    result = await broker.get_or_close(queue, keepalive_after=0.05)
    # Assert — the event is delivered, never shadowed by a beat.
    assert result["content"] == "hi"


@pytest.mark.asyncio
async def test_event_survives_repeated_keepalive_beats():
    # Arrange — THE safety property. Each beat abandons a pending ``Queue.get()``;
    # if that cancellation could consume an item, every keepalive interval would
    # be a chance to silently eat a message.
    broker = Broker()
    queue = await broker.subscribe("alice")
    for _ in range(5):
        await broker.get_or_close(queue, keepalive_after=0.02)
    # Act — publish AFTER five beats have come and gone, then read.
    await broker.publish("alice", mint_event("alice", content="survived"))
    result = await broker.get_or_close(queue, keepalive_after=1.0)
    # Assert
    assert result["content"] == "survived"


@pytest.mark.asyncio
async def test_closing_broker_returns_none_not_keepalive():
    # Arrange — a graceful shutdown must still stop the stream, beats or no.
    broker = Broker()
    queue = await broker.subscribe("alice")
    broker.close()
    # Act
    result = await broker.get_or_close(queue, keepalive_after=5.0)
    # Assert — None (stop), not KEEPALIVE (keep beating).
    assert result is None


@pytest.mark.asyncio
async def test_omitted_interval_keeps_two_state_contract():
    # Arrange — callers passing no interval must see the ORIGINAL contract: an
    # event, or None. A surprise third value would break them.
    broker = Broker()
    queue = await broker.subscribe("alice")
    broker.close()
    # Act
    result = await broker.get_or_close(queue)
    # Assert
    assert result is None


def test_interval_defaults_when_env_absent(no_keepalive_env):
    # Arrange — no override set.
    expected = DEFAULT_KEEPALIVE_INTERVAL_S
    # Act
    value = keepalive_interval_s()
    # Assert
    assert value == expected


def test_malformed_interval_env_falls_back_to_default(keepalive_env):
    # Arrange — a typo must NOT disable the beat: a disabled beat is the
    # silent-deafness footgun this whole mechanism exists to prevent.
    keepalive_env("not-a-number")
    # Act
    value = keepalive_interval_s()
    # Assert
    assert value == DEFAULT_KEEPALIVE_INTERVAL_S


def test_nonpositive_interval_env_falls_back_to_default(keepalive_env):
    # Arrange — same reasoning for 0 / negative: never buy "never beat again".
    keepalive_env("0")
    # Act
    value = keepalive_interval_s()
    # Assert
    assert value == DEFAULT_KEEPALIVE_INTERVAL_S


def test_valid_interval_env_overrides_default(keepalive_env):
    # Arrange — a deployment must be able to steer the cadence.
    keepalive_env("3.5")
    # Act
    value = keepalive_interval_s()
    # Assert
    assert value == 3.5
