from __future__ import annotations

import asyncio

from scitex_agent_container.runtimes import _hermes_inbox_bridge as bridge


def test_consumer_uses_authenticated_explicit_ack_after_turn_delivery(monkeypatch):
    seen = {}

    async def fake_push(_sink, event, **kwargs):
        seen["event"] = event
        seen["push"] = kwargs

    async def fake_consume(url, bearer, on_event, **kwargs):
        seen["stream"] = (url, bearer, kwargs)
        await on_event({"msg_id": "m-1", "content": "steer now"})

    monkeypatch.setenv("SAC_LISTEN_BEARER", "secret")
    monkeypatch.setattr(bridge, "_push_channel_event", fake_push)
    monkeypatch.setattr(bridge, "_consume_sse", fake_consume)

    asyncio.run(
        bridge.consume(
            name="scholar",
            listen_url="http://127.0.0.1:7878",
            turn_url="http://127.0.0.1:19001/v1/turn",
        )
    )

    assert seen == {
        "stream": (
            "http://127.0.0.1:7878/agents/scholar/inbox/stream?ack=explicit",
            "secret",
            {"ack_url": "http://127.0.0.1:7878/agents/scholar/inbox/ack"},
        ),
        "event": {"msg_id": "m-1", "content": "steer now"},
        "push": {
            "agent_name": "scholar",
            "listen_url": "http://127.0.0.1:7878",
            "bearer": "secret",
            "turn_url": "http://127.0.0.1:19001/v1/turn",
        },
    }


def test_consumer_refuses_to_subscribe_without_bearer(monkeypatch):
    monkeypatch.delenv("SAC_LISTEN_BEARER", raising=False)
    monkeypatch.setattr(bridge, "_read_listen_bearer", lambda: None)

    try:
        asyncio.run(
            bridge.consume(
                name="scholar",
                listen_url="http://127.0.0.1:7878",
                turn_url="http://127.0.0.1:19001/v1/turn",
            )
        )
    except RuntimeError as exc:
        assert "bearer is required" in str(exc)
    else:
        raise AssertionError("missing bearer must fail loud")
