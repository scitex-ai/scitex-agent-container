#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daemon liveness decider against the durable quota incident.

Covers :func:`make_daemon_state_fn` with hand-rolled stand-ins, never
mocks: a live incident reports BLOCKED under any beat writer, a stale
BLOCKED beat from before a verified clear does not survive recovery,
and malformed records fail closed the same way for pump and daemon.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from scitex_agent_container._runners._daemon_contract import make_daemon_state_fn
from scitex_agent_container._runners._incarnation import (
    WRITER_SESSION_DAEMON,
    WRITER_TURN_DRIVER,
)
from scitex_agent_container._runners._quota_incident import (
    clear_quota_incident,
    record_quota_incident,
)
from scitex_agent_container._runners._session_beat import (
    STATE_BLOCKED,
    STATE_BUSY,
    STATE_READY,
    write_heartbeat,
)

_ROUTE = "codex-sdk"
_TIME = 100.0
_TIMEOUT_S = 15


def _limited(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=_TIMEOUT_S))


async def _tick(decider):
    return decider()


def test_live_incident_reports_blocked_across_ticks(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    write_heartbeat(tmp_path, pid=1, state=STATE_BLOCKED, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    states = [_limited(_tick(decider)) for _ in range(2)]
    # Assert
    assert states == [STATE_BLOCKED, STATE_BLOCKED]


def test_periodic_writer_tick_keeps_blocked_while_incident_live(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    write_heartbeat(tmp_path, pid=1, state=STATE_BLOCKED, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    first = _limited(_tick(decider))
    write_heartbeat(tmp_path, pid=1, state=first, name="a", host=None, writer=WRITER_SESSION_DAEMON)
    second = _limited(_tick(decider))
    # Assert
    assert (first, second) == (STATE_BLOCKED, STATE_BLOCKED)


def test_verified_clear_releases_stale_blocked_beat(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    write_heartbeat(tmp_path, pid=1, state=STATE_BLOCKED, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}, route=_ROUTE
    )
    # Act
    clear_quota_incident(tmp_path, route=_ROUTE, evidence={"at": _TIME + 1.0, "kind": "probe-success"})
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_READY


def test_malformed_route_record_blocks_daemon(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text(
        json.dumps({"version": 1, "routes": {_ROUTE: None}}), encoding="utf-8"
    )
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}, route=_ROUTE
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_BLOCKED


def test_busy_turn_beat_preserved_while_task_runs(tmp_path):
    # Arrange
    write_heartbeat(tmp_path, pid=1, state=STATE_BUSY, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_BUSY


def test_ready_without_conversation_task(tmp_path):
    # Arrange
    decider = make_daemon_state_fn(tmp_path, stop=asyncio.Event(), convo_ref={})
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_READY
