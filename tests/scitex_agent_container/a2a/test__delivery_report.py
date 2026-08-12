#!/usr/bin/env python3
"""A publish that reached nobody must not look like one that landed.

`Broker.publish` returns how many live subscribers took the event. Callers threw
that number away, so a publish to an agent with no attached inbox was
indistinguishable from a delivered one.

Operator, 2026-08-08: 「送ったつもりで黙って失敗はありえないです」.

Two properties are under test here, and they pull in opposite directions:
a zero must be VISIBLE, and a success must be SILENT. Getting only the first
produces a log line per publish per agent, which buries the line that matters.

The second half of this file guards the INVARIANT rather than the helper: that
no `publish(...)` anywhere throws its count away again. Fixing five call sites
is a one-off; keeping them fixed is not, because the discarding form is shorter
and reads naturally, and it is what anyone adding a sixth site will write.

It lives HERE rather than in its own file on purpose. PS-204 §2 requires every
test file to mirror a source module, and a cross-cutting invariant has no module
to mirror — CI rejected a standalone `test__no_discarded_publish.py` as an orphan
test. That rule is right, and the invariant's natural home is beside the helper
whose use it enforces: `_delivery_report` exists so a zero is visible, and the
guard asserts every publish actually routes through it.

Real loggers via caplog. No mocks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from scitex_agent_container.a2a._delivery_report import report_zero_delivery

LOGGER_NAME = "test-delivery-report"
TARGET = "scitex-dev"

#: Modules that publish onto the inbox bus. Listed explicitly rather than
#: globbed: a glob would silently stop covering a file that moved, and a guard
#: that quietly checks nothing is worse than no guard.
PUBLISHING_MODULES = (
    "src/scitex_agent_container/a2a/_server.py",
    "src/scitex_agent_container/_listen/_node_channel.py",
    "src/scitex_agent_container/_lifecycle/_periodic_drive_loop.py",
)

#: A publish whose return value goes nowhere: the call is the whole statement.
#: Matches `await x.publish(...)` at the start of a line, which is precisely the
#: discarding form. A publish inside an assignment, an argument, or a `return`
#: keeps its value and does not match.
_DISCARDED = re.compile(r"^\s*await\s+[\w.]*publish\(", re.MULTILINE)

_ROOT = Path(__file__).resolve().parents[3]


def _source(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


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
            log, target=TARGET, what="test frame", delivered=0, row_id=4_242
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


# ---------------------------------------------------------------------------
# The invariant: no publish anywhere may discard its delivery count again.
#
# A SOURCE-TEXT GUARD IS AN UNUSUAL TEST AND THE CHOICE IS DELIBERATE. The
# alternative — standing up a Starlette app and a real broker per call site,
# then asserting on log records — exercises a great deal of machinery to test
# one line, and would still not fail when someone adds a sixth site. This fails
# exactly when the invariant breaks, names the file and line, and is instant.
#
# It is a REGRESSION guard, not a substitute for the behaviour tests above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", PUBLISHING_MODULES)
def test_no_publish_discards_its_delivery_count(rel: str) -> None:
    # Arrange: the discarding form is the one that reads most naturally, so it
    # is the one a future edit will reach for.
    text = _source(rel)
    # Act
    offenders = [text[: m.start()].count("\n") + 1 for m in _DISCARDED.finditer(text)]
    # Assert
    assert offenders == [], (
        f"{rel}: publish() result discarded at line(s) {offenders}. "
        "Pass it to a2a._delivery_report.report_zero_delivery, or hand the "
        "count back to the sender — a publish that reached nobody must not "
        "look like one that landed."
    )


@pytest.mark.parametrize("rel", PUBLISHING_MODULES)
def test_the_guarded_file_still_exists_and_publishes(rel: str) -> None:
    # Arrange: the POSITIVE CONTROL. Without it, a renamed or emptied module
    # passes the test above by containing no publishes at all — the guard would
    # go green precisely when it stopped guarding anything.
    text = _source(rel)
    # Act
    publishes = text.count("publish(")
    # Assert
    assert publishes > 0, f"{rel}: no publish() call found — has it moved?"
