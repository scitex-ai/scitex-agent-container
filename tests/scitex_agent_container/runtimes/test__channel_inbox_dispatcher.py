from __future__ import annotations

import asyncio

from scitex_agent_container.runtimes import _channel_inbox_dispatcher as bridge


def test_consumer_uses_authenticated_explicit_ack_after_turn_delivery():
    # Arrange
    seen = {}

    async def fake_push(_sink, event, **kwargs):
        seen["event"] = event
        seen["push"] = kwargs

    async def fake_consume(url, bearer, on_event, **kwargs):
        seen["stream"] = (url, bearer, kwargs)
        await on_event({"msg_id": "m-1", "content": "steer now"})

    async def fake_cards(**kwargs):
        seen["cards"] = kwargs

    # Act
    asyncio.run(
        bridge.consume(
            name="scholar",
            listen_url="http://127.0.0.1:7878",
            turn_url="http://127.0.0.1:19001/v1/turn",
            bearer="secret",
            push_event=fake_push,
            consume_sse=fake_consume,
            consume_cards_notifications=fake_cards,
        )
    )
    # Assert
    assert seen == {
        "stream": (
            "http://127.0.0.1:7878/agents/scholar/inbox/stream?ack=explicit",
            "secret",
            {"ack_url": "http://127.0.0.1:7878/agents/scholar/inbox/ack"},
        ),
        "event": {
            "msg_id": "m-1",
            "content": "steer now",
            "_require_terminal_visibility": True,
        },
        "push": {
            "agent_name": "scholar",
            "listen_url": "http://127.0.0.1:7878",
            "bearer": "secret",
            "turn_url": "http://127.0.0.1:19001/v1/turn",
        },
        "cards": {
            "name": "scholar",
            "turn_url": "http://127.0.0.1:19001/v1/turn",
            "bearer": "secret",
        },
    }


def test_consumer_refuses_to_subscribe_without_bearer():
    # Arrange
    error = None
    # Act
    try:
        asyncio.run(
            bridge.consume(
                name="scholar",
                listen_url="http://127.0.0.1:7878",
                turn_url="http://127.0.0.1:19001/v1/turn",
                environment={},
                resolve_bearer=lambda: None,
            )
        )
    except RuntimeError as exc:
        error = exc
    # Assert
    assert (type(error), str(error)) == (
        RuntimeError,
        "SAC listen bearer is required for channel inbox delivery",
    )
