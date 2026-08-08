#!/usr/bin/env python3
"""No `publish(...)` may have its delivery count thrown away.

`Broker.publish` returns the number of live subscribers that took the event.
Five call sites discarded it, so a send that reached nobody was indistinguishable
from one that landed. Operator, 2026-08-08:
「送ったつもりで黙って失敗はありえないです」.

Fixing five sites is a one-off. Keeping them fixed is not: the discarding form
(`await broker.publish(name, event)`) is shorter, reads naturally, and is what
anyone adding a sixth site will write. So the invariant is enforced here rather
than left as a convention in a review comment.

A SOURCE-TEXT GUARD IS AN UNUSUAL TEST AND THE CHOICE IS DELIBERATE. The
alternative — spinning a Starlette app and a real broker for each site, then
asserting on log records — tests the same one line through a great deal of
machinery, and would still not fail when someone adds site six. This fails
exactly when the invariant is broken, names the file and line, and costs
nothing to run.

It is a REGRESSION guard, not a substitute for behaviour tests: what
`report_zero_delivery` actually logs is pinned in `test__delivery_report.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
