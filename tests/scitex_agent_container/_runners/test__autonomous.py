"""Tests for the F-CS3 phase 2 autonomous-loop in claude_session runner."""

from __future__ import annotations

import asyncio

from scitex_agent_container._runners._session_inbox import TurnEnvelope
from scitex_agent_container._runners.claude_session import _autonomous_loop


def test_autonomous_loop_exits_on_drive_until_match():
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            env1: TurnEnvelope = await inbox.get()
            env1.response.set_result("still working")
            env2: TurnEnvelope = await inbox.get()
            env2.response.set_result("all good — DONE here")

        consumer_task = asyncio.create_task(consumer())
        rc = await _autonomous_loop(
            inbox,
            mission="kick off",
            drive_until="DONE",
            max_turns=10,
            kick_text="continue",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc, stop.is_set()

    rc, stopped = asyncio.run(scenario())
    assert rc == 0
    assert stopped is True


def test_autonomous_loop_caps_at_max_turns():
    seen: list[str] = []

    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            for _ in range(3):
                env: TurnEnvelope = await inbox.get()
                seen.append(env.text)
                env.response.set_result("not done yet")

        consumer_task = asyncio.create_task(consumer())
        rc = await _autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=3,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc, stop.is_set()

    rc, stopped = asyncio.run(scenario())
    assert rc == 1
    assert stopped is True
    assert seen[0] == "seed"
    assert seen[1:] == ["kick", "kick"]


def test_autonomous_loop_stops_when_event_set_before_loop_starts():
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        stop.set()
        loop = asyncio.get_running_loop()
        rc = await _autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=5,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        return rc, inbox.empty()

    rc, empty = asyncio.run(scenario())
    assert rc == 1
    assert empty is True
