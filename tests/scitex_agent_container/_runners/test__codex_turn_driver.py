#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn-drain admission, conservation and waiter lifecycle.

Covers the ``_codex_turn_driver`` drain loop with hand-rolled stand-ins
(the ``_Scripted*`` idiom), never mocks: held envelopes wait in arrival
order without touching the backend, clear alone releases them, exits
settle every envelope, and owned waiter tasks never leak.
"""

from __future__ import annotations

import asyncio
from scitex_agent_container._runners._codex_turn_driver import (
    _drain_codex_inbox,
    _wait_arrival,
)
from scitex_agent_container._runners._harness_session import NormalizedEvent, RunResult
from scitex_agent_container._runners._quota_incident import (
    clear_quota_incident,
    record_quota_incident,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)

_ROUTE = "codex-sdk"
_TIME = 100.0
_TIMEOUT_S = 15


def _limited(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=_TIMEOUT_S))


def _good_evidence(at=_TIME + 1.0):
    return {"at": at, "kind": "probe-success"}


class _ScriptedBackend:
    """Counts backend turns; answers every turn with a canned result."""

    def __init__(self):
        self.texts = []

    async def send(self, message):
        self.texts.append(message.content)
        yield NormalizedEvent(kind="text_delta", text="ok")
        yield NormalizedEvent(
            kind="result",
            result=RunResult(text="ok", session_id="thr", usage={}),
        )


def _live_turn(text="hi"):
    return TurnEnvelope(text=text, response=asyncio.get_running_loop().create_future())


def test_denied_turn_creates_no_backend_turn(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        await inbox.put(_live_turn("held"))
        await inbox.put(ShutdownEnvelope())
        await _drain_codex_inbox(
            backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
            print_stream=False, name="agent", host=None,
            shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
        )
        return backend

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == []


def test_held_envelope_fails_honestly_at_shutdown(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    outcomes = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        env = _live_turn("held")
        await inbox.put(env)
        await inbox.put(ShutdownEnvelope())
        await _drain_codex_inbox(
            _ScriptedBackend(), inbox, state_dir=tmp_path, pid=1, stop=stop,
            print_stream=False, name="agent", host=None,
            shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
        )
        try:
            env.response.result()
        except BaseException as exc:  # noqa: BLE001 — asserting the failure shape
            outcomes.append(type(exc).__name__)

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == ["QuotaBlockedError"]


def test_clear_releases_held_turns_without_new_arrival(tmp_path):
    # Arrange — the driver is ALREADY running while the route is blocked.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()
    results: list = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        first = _live_turn("first")
        second = _live_turn("second")
        await inbox.put(first)
        await inbox.put(second)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        pending = None
        try:
            for _ in range(300):
                if backend.texts == [] and inbox.empty():
                    break
                await asyncio.sleep(0.01)
            clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
            pending = asyncio.gather(first.response, second.response)
            answers = await asyncio.wait_for(asyncio.shield(pending), timeout=5.0)
            results.append(tuple(answers))
        finally:
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(drain, timeout=5.0)
            except TimeoutError:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            for env in (first, second):
                if env.response.done() and not env.response.cancelled():
                    env.response.exception()

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == ["first", "second"]


def test_cleared_held_turns_keep_arrival_order(tmp_path):
    # Arrange — two held turns, cleared with no new arrival.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()
    results: list = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        first = _live_turn("first")
        second = _live_turn("second")
        await inbox.put(first)
        await inbox.put(second)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        pending = None
        try:
            for _ in range(300):
                if backend.texts == [] and inbox.empty():
                    break
                await asyncio.sleep(0.01)
            clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
            pending = asyncio.gather(first.response, second.response)
            answers = await asyncio.wait_for(asyncio.shield(pending), timeout=5.0)
            results.append(tuple(answers))
        finally:
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(drain, timeout=5.0)
            except TimeoutError:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            for env in (first, second):
                if env.response.done() and not env.response.cancelled():
                    env.response.exception()

    # Act
    _limited(_scenario())
    # Assert
    assert results == [("ok", "ok")]


def test_cancel_conserves_held_envelopes(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    outcomes = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        env = _live_turn("held")
        await inbox.put(env)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                _ScriptedBackend(), inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        await asyncio.sleep(0.2)
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass
        try:
            env.response.result()
        except BaseException as exc:  # noqa: BLE001 — asserting conservation
            outcomes.append(type(exc).__name__)

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == ["QuotaBlockedError"]


def test_stop_settles_consumed_envelope_without_backend_turn(tmp_path):
    # Arrange
    inbox = asyncio.Queue()
    stop = asyncio.Event()
    outcomes = []

    class _UnusedBackend:
        async def send(self, message):
            raise AssertionError("a stopped driver must not create a backend turn")
            yield  # pragma: no cover — async iterator shape only

    async def _scenario():
        loop = asyncio.get_running_loop()
        env = TurnEnvelope(text="late", response=loop.create_future())
        await inbox.put(env)
        stop.set()
        await _drain_codex_inbox(
            _UnusedBackend(), inbox, state_dir=tmp_path, pid=1, stop=stop,
            print_stream=False, name="agent", host=None,
            shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
        )
        outcomes.append(env.response.done())

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == [True]


def test_completed_persistent_getter_is_consumed_exactly_once():
    # Arrange
    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        envelope = object()
        await inbox.put(envelope)
        getter = asyncio.create_task(inbox.get())
        await getter
        slot = [getter]
        stop = asyncio.Event()
        try:
            first, first_stopped = await _wait_arrival(inbox, stop, slot)
            second, second_stopped = await _wait_arrival(inbox, stop, slot)
            return first, first_stopped, second, second_stopped, envelope
        finally:
            pending = slot[0]
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)

    # Act
    first, first_stopped, second, second_stopped, envelope = _limited(_scenario())
    # Assert
    assert (first is envelope, first_stopped, second, second_stopped) == (
        True,
        False,
        None,
        False,
    )


def test_arrival_retires_its_stop_waiter_before_returning():
    # Arrange
    async def _scenario():
        original = asyncio.all_tasks()
        inbox: asyncio.Queue = asyncio.Queue()
        await inbox.put(object())
        stop = asyncio.Event()
        slot = [None]
        try:
            item, stopped = await _wait_arrival(inbox, stop, slot)
            leaked = asyncio.all_tasks() - original
            return item, stopped, leaked
        finally:
            stop.set()
            leaked = asyncio.all_tasks() - original
            await asyncio.gather(*leaked, return_exceptions=True)

    # Act
    item, stopped, leaked = _limited(_scenario())
    # Assert
    assert (item is not None, stopped, leaked) == (True, False, set())


def test_driver_stop_awaits_its_cancelled_getter_cleanup(tmp_path):
    # Arrange
    async def _scenario():
        original = asyncio.all_tasks()
        stop = asyncio.Event()
        stop.set()
        try:
            await _drain_codex_inbox(
                object(), asyncio.Queue(), state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
            return asyncio.all_tasks() - original == set()
        finally:
            leaked = asyncio.all_tasks() - original
            for task in leaked:
                task.cancel()
            await asyncio.gather(*leaked, return_exceptions=True)

    # Act
    clean = _limited(_scenario())
    # Assert
    assert clean is True
