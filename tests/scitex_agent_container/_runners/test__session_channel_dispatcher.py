from __future__ import annotations

import asyncio

from scitex_agent_container._runners._session_channel_dispatcher import (
    dispatch_to_session,
)
from scitex_agent_container._runners._session_inbox import make_inbox
from scitex_agent_container.runtimes import _cards_ingress


def test_durable_event_is_acked_only_after_harness_future_resolves():
    # Arrange
    observations = {}

    async def scenario():
        inbox = make_inbox()
        dispatch = asyncio.create_task(
            dispatch_to_session(
                {
                    "msg_id": "m-1",
                    "from_agent": "lead",
                    "dispatch_id": "d-1",
                    "content": "inspect signup",
                },
                inbox=inbox,
            )
        )
        envelope = await inbox.get()
        observations["before"] = dispatch.done()
        observations["identity"] = (
            envelope.from_agent,
            envelope.dispatch_id,
            "inspect signup" in envelope.text,
        )
        envelope.response.set_result("accepted by harness")
        await dispatch

    # Act
    asyncio.run(scenario())
    # Assert
    assert observations == {
        "before": False,
        "identity": ("lead", "d-1", True),
    }


def test_harness_failure_reaches_durable_transport_without_ack():
    # Arrange
    async def scenario():
        inbox = make_inbox()
        dispatch = asyncio.create_task(
            dispatch_to_session(
                {"msg_id": "m-2", "content": "do not lose me"}, inbox=inbox
            )
        )
        envelope = await inbox.get()
        envelope.response.set_exception(RuntimeError("native steer rejected"))
        try:
            await dispatch
        except (
            RuntimeError
        ) as exc:  # stx-allow: test-capture (reason: assertion target)
            return str(exc)
        return None

    # Act
    error = asyncio.run(scenario())
    # Assert
    assert error == "native steer rejected"


def test_cards_confirmation_follows_common_harness_receipt():
    # Arrange
    calls = []
    observations = {}

    def poll(_agent, **_kwargs):
        return {
            "store": "postgresql://cards-primary",
            "unconfirmed": ["n-1"],
            "notifications": [{"id": "n-1", "actor": "lead", "body": "inspect signup"}],
        }

    def ack(_agent, ids, **_kwargs):
        calls.append(("ack", ids))
        return {"confirmed": ids}

    async def scenario():
        inbox = make_inbox()

        async def deliver(event, **_transport):
            await dispatch_to_session(event, inbox=inbox)

        drain = asyncio.create_task(
            _cards_ingress.drain_once(
                name="hub",
                turn_url="direct://resident-session",
                bearer=None,
                poll_notifications=poll,
                ack_notifications=ack,
                deliver=deliver,
            )
        )
        envelope = await inbox.get()
        observations["before"] = list(calls)
        envelope.response.set_result("done")
        observations["drained"] = await drain

    # Act
    asyncio.run(scenario())
    # Assert
    assert (observations, calls) == (
        {"before": [], "drained": 1},
        [("ack", ["n-1"])],
    )
