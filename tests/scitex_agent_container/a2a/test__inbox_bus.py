"""Tests for the per-agent inbox pub/sub broker (commit 1 of the
A2A push channel slice). See docs/sac-and-orochi.md.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.a2a._inbox_bus import (
    DAEMON_SENDER,
    Broker,
    mint_acl_deny_synthetic_notification,
    mint_deny_notification,
    mint_event,
)

# ---------------------------------------------------------------------------
# Broker.publish / subscribe behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_returns_zero_delivered() -> None:
    # Arrange
    broker = Broker()
    # Act
    delivered = await broker.publish("nobody", {"x": 1})
    # Assert
    assert delivered == 0


@pytest.mark.asyncio
async def test_single_subscriber_publish_reports_one_delivery() -> None:
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    # Act
    delivered = await broker.publish("alice", {"x": 1})
    # Assert
    assert delivered == 1


@pytest.mark.asyncio
async def test_single_subscriber_queue_receives_payload() -> None:
    # Arrange
    broker = Broker()
    q = await broker.subscribe("alice")
    # Act
    await broker.publish("alice", {"x": 1})
    # Assert
    assert q.get_nowait() == {"x": 1}


@pytest.mark.asyncio
async def test_publish_fanout_reports_total_subscriber_count() -> None:
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    await broker.subscribe("alice")
    # Act
    delivered = await broker.publish("alice", {"x": 2})
    # Assert
    assert delivered == 2


@pytest.mark.asyncio
async def test_publish_fanout_delivers_to_first_subscriber_queue() -> None:
    # Arrange
    broker = Broker()
    q1 = await broker.subscribe("alice")
    await broker.subscribe("alice")
    # Act
    await broker.publish("alice", {"x": 2})
    # Assert
    assert q1.get_nowait() == {"x": 2}


@pytest.mark.asyncio
async def test_publish_fanout_delivers_to_second_subscriber_queue() -> None:
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    q2 = await broker.subscribe("alice")
    # Act
    await broker.publish("alice", {"x": 2})
    # Assert
    assert q2.get_nowait() == {"x": 2}


@pytest.mark.asyncio
async def test_publish_delivers_to_addressed_agent_queue() -> None:
    # Arrange
    broker = Broker()
    alice_q = await broker.subscribe("alice")
    await broker.subscribe("bob")
    # Act
    await broker.publish("alice", {"x": "for-alice"})
    # Assert
    assert alice_q.get_nowait() == {"x": "for-alice"}


@pytest.mark.asyncio
async def test_publish_does_not_leak_to_other_agent_queue() -> None:
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    bob_q = await broker.subscribe("bob")
    # Act
    await broker.publish("alice", {"x": "for-alice"})
    # Assert
    assert bob_q.empty()


@pytest.mark.asyncio
async def test_unsubscribe_then_publish_reports_zero_delivered() -> None:
    # Arrange
    broker = Broker()
    q = await broker.subscribe("alice")
    await broker.unsubscribe("alice", q)
    # Act
    delivered = await broker.publish("alice", {"x": 3})
    # Assert
    assert delivered == 0


@pytest.mark.asyncio
async def test_unsubscribe_one_keeps_remaining_subscriber_attached() -> None:
    # Arrange two subscribers so unsubscribing one leaves a non-empty set.
    broker = Broker()
    q1 = await broker.subscribe("alice")
    await broker.subscribe("alice")
    # Act unsubscribe only one — the agent key must NOT be popped.
    await broker.unsubscribe("alice", q1)
    delivered = await broker.publish("alice", {"x": 9})
    # Assert remaining subscriber still receives events.
    assert delivered == 1


@pytest.mark.asyncio
async def test_subscriber_count_reflects_active_subscriptions() -> None:
    # Arrange
    broker = Broker()
    await broker.subscribe("alice")
    await broker.subscribe("alice")
    # Act
    count = await broker.subscriber_count("alice")
    # Assert
    assert count == 2


# ---------------------------------------------------------------------------
# Bounded-queue / slow-consumer policy: when more than 64 events are published
# to a subscriber that never drains, the broker keeps the newest 64 and drops
# the oldest. Three independent properties are exercised below.
# ---------------------------------------------------------------------------


@pytest.fixture
def overflowed_queue_drain():
    """Publish 70 events to a single subscriber without draining and return
    the resulting drained list. The producer side runs synchronously inside
    the asyncio event loop driven by pytest-asyncio.
    """
    import asyncio

    async def _build():
        broker = Broker()
        q = await broker.subscribe("alice")
        for i in range(70):
            await broker.publish("alice", {"i": i})
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        return drained

    return asyncio.run(_build())


def test_slow_consumer_queue_caps_at_sixty_four_events(
    overflowed_queue_drain,
) -> None:
    # Arrange
    drained = overflowed_queue_drain
    # Act
    size = len(drained)
    # Assert
    assert size == 64


def test_slow_consumer_keeps_newest_event_after_overflow(
    overflowed_queue_drain,
) -> None:
    # Arrange
    drained = overflowed_queue_drain
    # Act
    newest_index = drained[-1]["i"]
    # Assert
    assert newest_index == 69


def test_slow_consumer_drops_oldest_events_after_overflow(
    overflowed_queue_drain,
) -> None:
    # Arrange
    drained = overflowed_queue_drain
    # Act
    oldest_index = drained[0]["i"]
    # Assert
    assert oldest_index >= 6


# ---------------------------------------------------------------------------
# mint_event: shape of returned dict
# ---------------------------------------------------------------------------


@pytest.fixture
def full_minted_event():
    return mint_event(
        "alice",
        content="hello",
        from_agent="bob",
        conversation_id="c1",
        in_reply_to="m0",
        priority="high",
        requires_reply=True,
    )


@pytest.mark.parametrize(
    "field,expected",
    [
        ("to_agent", "alice"),
        ("from_agent", "bob"),
        ("content", "hello"),
        ("conversation_id", "c1"),
        ("in_reply_to", "m0"),
        ("priority", "high"),
        ("requires_reply", True),
    ],
)
def test_mint_event_populates_explicit_field(
    full_minted_event, field, expected
) -> None:
    # Arrange
    event = full_minted_event
    # Act
    actual = event[field]
    # Assert
    assert actual == expected


def test_mint_event_generates_msg_id_string_of_min_length(
    full_minted_event,
) -> None:
    # Arrange
    event = full_minted_event
    # Act
    msg_id = event["msg_id"]
    # Assert
    assert isinstance(msg_id, str) and len(msg_id) >= 16


def test_mint_event_timestamp_is_float(full_minted_event) -> None:
    # Arrange
    event = full_minted_event
    # Act
    ts = event["ts"]
    # Assert
    assert isinstance(ts, float)


# ---------------------------------------------------------------------------
# mint_event: default values when optional kwargs are omitted
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_minted_event():
    return mint_event("alice", content="hi")


@pytest.mark.parametrize(
    "field,expected",
    [
        ("from_agent", "unknown"),
        ("priority", "normal"),
        ("requires_reply", False),
    ],
)
def test_mint_event_default_value_for_optional_field(
    minimal_minted_event, field, expected
) -> None:
    # Arrange
    event = minimal_minted_event
    # Act
    actual = event[field]
    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    "field",
    ["in_reply_to", "conversation_id"],
)
def test_mint_event_omits_unset_optional_field(minimal_minted_event, field) -> None:
    # Arrange
    event = minimal_minted_event
    # Act
    present = field in event
    # Assert
    assert present is False


def test_mint_event_attaches_extra_metadata_when_provided() -> None:
    # Arrange
    event = mint_event("alice", content="hi", extra={"trace_id": "abc"})
    # Act
    extra = event.get("extra")
    # Assert
    assert extra == {"trace_id": "abc"}


# ---------------------------------------------------------------------------
# mint_event: ``ack`` survives minting so the auto-ack loop-guard works
# ---------------------------------------------------------------------------


def test_mint_event_carries_ack_flag_when_true() -> None:
    # Arrange
    event = mint_event("alice", content="read", from_agent="bob", ack=True)
    # Act
    ack = event["ack"]
    # Assert
    assert ack is True


def test_mint_event_defaults_ack_to_false() -> None:
    # Arrange
    event = mint_event("alice", content="hi", from_agent="bob")
    # Act
    ack = event["ack"]
    # Assert
    assert ack is False


# ---------------------------------------------------------------------------
# mint_event: ``dispatch_id`` rides on the event so the channel wake path
# can thread it onto the woken turn for requester-completion correlation.
# ---------------------------------------------------------------------------


def test_mint_event_carries_dispatch_id_when_provided() -> None:
    # Arrange
    event = mint_event("alice", content="hi", from_agent="bob", dispatch_id="d-123")
    # Act
    dispatch_id = event["dispatch_id"]
    # Assert
    assert dispatch_id == "d-123"


def test_mint_event_omits_dispatch_id_when_absent() -> None:
    # Arrange
    event = mint_event("alice", content="hi", from_agent="bob")
    # Act
    present = "dispatch_id" in event
    # Assert
    assert present is False


# ---------------------------------------------------------------------------
# mint_deny_notification — receiver-side ACL-deny notice (comms item D).
# Shape contract: marked ``kind="denied_attempt"``, empty content,
# from/to/reason carried, no message body leak.
# ---------------------------------------------------------------------------


def test_mint_deny_notification_marks_kind_denied_attempt() -> None:
    # Arrange
    event = mint_deny_notification(
        target="alice", from_agent="mallory", reason="cross-group"
    )
    # Act
    kind = event.get("kind")
    # Assert
    assert kind == "denied_attempt"


def test_mint_deny_notification_carries_from_agent() -> None:
    # Arrange
    event = mint_deny_notification(
        target="alice",
        from_agent="mallory",
        reason="cross-group: mallory may not address alice",
    )
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent == "mallory"


def test_mint_deny_notification_carries_to_agent() -> None:
    # Arrange
    event = mint_deny_notification(
        target="alice",
        from_agent="mallory",
        reason="cross-group: mallory may not address alice",
    )
    # Act
    to_agent = event["to_agent"]
    # Assert
    assert to_agent == "alice"


def test_mint_deny_notification_carries_deny_reason_verbatim() -> None:
    # Arrange
    event = mint_deny_notification(
        target="alice",
        from_agent="mallory",
        reason="cross-group: mallory may not address alice",
    )
    # Act
    reason = event["extra"]["deny_reason"]
    # Assert
    assert reason == "cross-group: mallory may not address alice"


def test_mint_deny_notification_has_empty_content() -> None:
    """Body must NEVER leak — only attempt metadata."""
    # Arrange
    event = mint_deny_notification(
        target="alice", from_agent="mallory", reason="cross-group"
    )
    # Act
    content = event.get("content", None)
    # Assert
    assert content == ""


def test_mint_deny_notification_records_unknown_when_no_sender_identity() -> None:
    """The 'no identity at all' deny path: receiver gets ``unknown`` —
    that's the only honest answer, never a fabricated name.
    """
    # Arrange
    event = mint_deny_notification(
        target="alice", from_agent=None, reason="no authenticated identity"
    )
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent == "unknown"


def test_mint_deny_notification_includes_timestamp() -> None:
    # Arrange
    event = mint_deny_notification(
        target="alice", from_agent="mallory", reason="cross-group"
    )
    # Act
    ts = event.get("ts")
    # Assert
    assert isinstance(ts, float) and ts > 0


# ---------------------------------------------------------------------------
# DAEMON_SENDER — canonical ``from_agent`` (sender) value for messages sac's
# OWN daemon originates (operator directive 2026-07-05, bracket form). The
# channel tag renders ``<- <clean-source> [<sender>]``; a daemon frame's
# sender is ``"daemon"`` (renders ``<- sac [daemon]``). The source stays the
# clean, UNSUFFIXED channel name — no ``*-system`` token. Sender-supplied
# sources (agent a2a from_agent, other channels' tags) MUST pass through
# unchanged.
# ---------------------------------------------------------------------------


def test_daemon_sender_constant_value() -> None:
    # Arrange — pin the exact wire literal the operator's bracket
    # convention depends on.
    expected = "daemon"
    # Act
    actual = DAEMON_SENDER
    # Assert
    assert actual == expected


def test_acl_deny_synthetic_uses_daemon_sender() -> None:
    # Arrange — a sac-originated daemon notification (no sender-supplied
    # source) must be tagged with the canonical daemon sender.
    event = mint_acl_deny_synthetic_notification(
        target="alice", sender="mallory", reason="cross-group"
    )
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent == DAEMON_SENDER


def test_acl_deny_synthetic_is_not_bare_system() -> None:
    # Arrange — regression guard: the old bare ``"system"`` tag must no
    # longer appear on sac's own daemon messages.
    event = mint_acl_deny_synthetic_notification(
        target="alice", sender="mallory", reason="cross-group"
    )
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent != "system"


def test_daemon_sender_is_not_package_suffixed() -> None:
    # Arrange — regression guard: the sender must NOT be a package-
    # suffixed ``*-system`` token; the source stays clean and the
    # bracket carries the plain ``daemon`` sender.
    event = mint_acl_deny_synthetic_notification(
        target="alice", sender="mallory", reason="cross-group"
    )
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert not from_agent.endswith("-system")


def test_mint_event_passes_through_sender_supplied_source_unchanged() -> None:
    # Arrange — a message that arrives WITH a sender-supplied source
    # (e.g. another channel's tag such as scitex-todo's) must be
    # delivered with THAT source unchanged; sac only applies its own
    # daemon sender to messages it originates with no sender-supplied
    # source.
    event = mint_event("alice", content="hi", from_agent="scitex-todo-system")
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent == "scitex-todo-system"


def test_mint_event_does_not_override_agent_sender_with_daemon() -> None:
    # Arrange — an agent's own a2a ``from_agent`` must never be re-tagged
    # to the daemon sender; it keeps its agent identity in the bracket.
    event = mint_event("alice", content="hi", from_agent="worker-b")
    # Act
    from_agent = event["from_agent"]
    # Assert
    assert from_agent == "worker-b"
