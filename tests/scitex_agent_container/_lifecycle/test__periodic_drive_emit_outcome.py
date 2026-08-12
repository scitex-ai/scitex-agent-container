"""A scheduled publish that reached nobody must not look like one that landed.

The periodic drive scheduled its `Broker.publish` with a bare
`asyncio.create_task`, which discards the coroutine's return value AND any
exception it raised. `publish` returns the number of live subscribers that took
the event, so a drive to a stopped agent returned 0 into a void and read exactly
like a delivered one.

Operator, 2026-08-08: 「送ったつもりで黙って失敗はありえないです」.

A zero here is INFO rather than an error, deliberately: the drive is a nudge and
a stopped agent is a normal state. What was unacceptable was that the two
outcomes were indistinguishable, not that one of them occurs.

Real asyncio tasks and a real logger via caplog. No mocks.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from scitex_agent_container._lifecycle._periodic_drive_loop import _log_emit_outcome

AGENT = "scitex-agent-container"
LOGGER_NAME = "test-emit-outcome"


async def _returning(value: int) -> int:
    return value


async def _raising() -> int:
    raise RuntimeError("broker went away")


async def _completed(coro) -> asyncio.Task:
    """Run ``coro`` as a task to completion and hand back the finished task."""
    task = asyncio.ensure_future(coro)
    await asyncio.gather(task, return_exceptions=True)
    return task


def _messages(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


@pytest.mark.asyncio
async def test_zero_delivery_is_reported(caplog) -> None:
    # Arrange: publish took the event to no live subscriber.
    log = logging.getLogger(LOGGER_NAME)
    task = await _completed(_returning(0))
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert any(AGENT in m for m in _messages(caplog, logging.INFO))


@pytest.mark.asyncio
async def test_zero_delivery_is_info_not_error(caplog) -> None:
    # Arrange: a stopped agent is a normal state; shouting would train the
    # reader to ignore the line by the time it matters.
    log = logging.getLogger(LOGGER_NAME)
    task = await _completed(_returning(0))
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert _messages(caplog, logging.ERROR) == []


@pytest.mark.asyncio
async def test_a_successful_delivery_is_silent(caplog) -> None:
    # Arrange: the common case must not add a line per tick per agent.
    log = logging.getLogger(LOGGER_NAME)
    task = await _completed(_returning(3))
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert caplog.records == []


@pytest.mark.asyncio
async def test_an_exception_is_reported_as_an_error(caplog) -> None:
    # Arrange: a bare create_task swallowed this entirely.
    log = logging.getLogger(LOGGER_NAME)
    task = await _completed(_raising())
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert any("broker went away" in m for m in _messages(caplog, logging.ERROR))


@pytest.mark.asyncio
async def test_reporting_an_exception_returns_rather_than_raising(caplog) -> None:
    # Arrange: the callback runs on the event loop; raising there would take
    # down something unrelated to the send it was reporting on. Asserting the
    # log line landed proves it ran to completion — "it did not raise" alone
    # would also be true of a function that did nothing.
    log = logging.getLogger(LOGGER_NAME)
    task = await _completed(_raising())
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert len(_messages(caplog, logging.ERROR)) == 1


@pytest.mark.asyncio
async def test_a_cancelled_emit_is_reported(caplog) -> None:
    # Arrange: cancellation is neither success nor failure, and calling
    # .exception() on a cancelled task raises — so it needs its own branch.
    log = logging.getLogger(LOGGER_NAME)
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        _log_emit_outcome(log, AGENT, task)
    # Assert
    assert any("cancelled" in m for m in _messages(caplog, logging.INFO))
