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
            "deliver": seen["cards"]["deliver"],
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


def test_daemon_dispatches_sac_and_cards_through_one_target_adapter():
    # Arrange
    seen = []

    async def dispatch(event):
        seen.append((event["msg_id"], event["_require_terminal_visibility"]))

    async def consume_sse(_url, _bearer, on_event, **_kwargs):
        await on_event({"msg_id": "sac-1", "content": "from sac"})

    async def consume_cards(*, deliver, **_kwargs):
        await deliver({"msg_id": "cards-1", "content": "from cards"})

    async def forbidden_push(*_args, **_kwargs):
        raise AssertionError("transport-specific push must not bypass adapter")

    # Act
    asyncio.run(
        bridge.consume(
            name="scholar",
            listen_url="http://127.0.0.1:7878",
            turn_url="direct://resident-session",
            bearer="secret",
            channels=("server:sac", "server:scitex-cards"),
            consume_sse=consume_sse,
            consume_cards_notifications=consume_cards,
            push_event=forbidden_push,
            dispatch_event=dispatch,
        )
    )

    # Assert
    assert set(seen) == {("sac-1", True), ("cards-1", True)}


def test_completed_owned_card_marks_fresh_next_task_after_delivery(tmp_path):
    # Arrange
    delivered = []
    (tmp_path / "hermes-active-card.json").write_text(
        '{"card_id":"card-7"}', encoding="utf-8"
    )
    (tmp_path / "hermes-owned-session.json").write_text(
        '{"live_session_id":"live-old","stored_session_id":"stored-old"}',
        encoding="utf-8",
    )

    async def dispatch(event):
        delivered.append(event["msg_id"])

    async def consume_sse(_url, _bearer, on_event, **_kwargs):
        await on_event(
            {
                "msg_id": "done-1",
                "kind": "card-event",
                "from_agent": "scitex-cards",
                "extra": {
                    "card_id": "card-7",
                    "card_event_kind": "completed",
                    "card_event_owner": "scholar",
                },
            }
        )

    # Act
    asyncio.run(
        bridge.consume(
            name="scholar",
            listen_url="http://127.0.0.1:7878",
            turn_url="direct://resident-session",
            bearer="secret",
            channels=("server:sac",),
            consume_sse=consume_sse,
            dispatch_event=dispatch,
            state_dir=tmp_path,
        )
    )
    # Assert
    assert (delivered, (tmp_path / "hermes-fresh-next-task.json").exists()) == (
        ["done-1"],
        True,
    )
