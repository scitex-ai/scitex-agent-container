#!/usr/bin/env python3
"""A publish that reached nobody must not look like one that landed.

`Broker.publish` returns how many live subscribers took the event. Callers threw
that number away, so a publish to an agent with no attached inbox was
indistinguishable from a delivered one.

Operator, 2026-08-08: 「送ったつもりで黙って失敗はありえないです」.

Two properties are under test here, and they pull in opposite directions:
a zero must be VISIBLE, and a success must be SILENT. Getting only the first
produces a log line per publish per agent, which buries the line that matters.

Real loggers via caplog. No mocks.
"""

from __future__ import annotations

import logging

from scitex_agent_container.a2a._delivery_report import report_zero_delivery

LOGGER_NAME = "test-delivery-report"
TARGET = "scitex-dev"


def _messages(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_zero_delivery_is_reported(caplog) -> None:
    # Arrange: nobody was subscribed when the event was published.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="test frame", delivered=0)
    # Assert
    assert any(TARGET in m for m in _messages(caplog, logging.INFO))


def test_zero_delivery_names_the_kind_of_frame(caplog) -> None:
    # Arrange: by the time someone reads this line, the useful question is
    # WHICH notification the agent missed — not which source line emitted it.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="approval prompt", delivered=0)
    # Assert
    assert any("approval prompt" in m for m in _messages(caplog, logging.INFO))


def test_zero_delivery_is_info_not_error(caplog) -> None:
    # Arrange: a stopped agent is a NORMAL state, and the row is durable. An
    # error per publish per stopped agent trains the reader to skip the line.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="test frame", delivered=0)
    # Assert
    assert _messages(caplog, logging.ERROR) == []


def test_the_durable_row_id_is_reported(caplog) -> None:
    # Arrange: the row id is what makes the line actionable — it says the event
    # survived and names the row to go and look at.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(
            log, target=TARGET, what="test frame", delivered=0, row_id=4242
        )
    # Assert
    assert any("4242" in m for m in _messages(caplog, logging.INFO))


def test_without_a_row_id_it_does_not_claim_durability(caplog) -> None:
    # Arrange: a caller with no persisted row must not get a line promising the
    # event replays on reconnect. That promise would be false.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="test frame", delivered=0)
    # Assert
    assert not any("replays" in m for m in _messages(caplog, logging.INFO))


def test_a_successful_delivery_is_silent(caplog) -> None:
    # Arrange: the common case must not add a line per publish per agent.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="test frame", delivered=3)
    # Assert
    assert caplog.records == []


def test_a_successful_delivery_reports_that_it_said_nothing() -> None:
    # Arrange: the return value lets a caller branch without parsing records.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    reported = report_zero_delivery(log, target=TARGET, what="x", delivered=1)
    # Assert
    assert reported is False


def test_a_zero_reports_that_it_spoke() -> None:
    # Arrange: the other half of the same contract.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    reported = report_zero_delivery(log, target=TARGET, what="x", delivered=0)
    # Assert
    assert reported is True


def test_none_is_treated_as_a_zero(caplog) -> None:
    # Arrange: a caller whose publish could not report at all hands us None.
    # Silence there would be the original defect wearing a different type.
    log = logging.getLogger(LOGGER_NAME)
    # Act
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report_zero_delivery(log, target=TARGET, what="test frame", delivered=None)
    # Assert
    assert _messages(caplog, logging.INFO) != []
