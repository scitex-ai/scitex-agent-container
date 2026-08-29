"""Tests for the dispatch-ledger reaction-ack additions
(``feat/comm-reaction-ack``, 2026-06-14).

Operator mandate (lead a2a ``1781e82a``): "absence of the reaction
= comm miss, detectable". This module covers the persistence half:

* :data:`STATUS_REACTED` is registered as a valid lifecycle status —
  ``record_dispatch`` does not reject it and ``mark_dispatch_reacted``
  writes it durably.
* :func:`mark_dispatch_reacted` is the thin wrapper the sender's
  channel adapter calls when a structural 👀 receipt arrives. It is
  idempotent (a second call leaves the row at REACTED).
* :func:`list_unreacted_dispatches` returns the rows older than the
  SLO that have NOT been REACTED — the comm-miss surface. Terminal
  statuses (``reacted``, ``failed``, ``timeout``) are excluded.

Conventions: AAA markers, one assertion per test (STX-TQ007), no mocks / no
monkeypatch. Real PostgreSQL via ``scitex_dev.store``, isolated per test by
the ``pg_schema`` fixture — ``db_path`` is gone, because it named a SQLite
file and there is no file.
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------------------
# STATUS_REACTED is a registered lifecycle status.
# ---------------------------------------------------------------------------


def test_status_reacted_is_registered_as_valid():
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        VALID_STATUSES,
    )

    # Act
    is_valid = STATUS_REACTED in VALID_STATUSES
    # Assert
    assert is_valid is True


def test_record_dispatch_accepts_status_reacted(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        record_dispatch,
    )

    # Act
    record_dispatch(
        from_agent="alice",
        to_agent="bob",
        text="hi",
        status=STATUS_REACTED,
    )
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_REACTED


# ---------------------------------------------------------------------------
# mark_dispatch_reacted — writes STATUS_REACTED on an existing row.
# ---------------------------------------------------------------------------


def test_mark_dispatch_reacted_flips_status_to_reacted(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        mark_dispatch_reacted,
        record_dispatch,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    mark_dispatch_reacted(did)
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_REACTED


def test_mark_dispatch_reacted_returns_true_on_match(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        mark_dispatch_reacted,
        record_dispatch,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    matched = mark_dispatch_reacted(did)
    # Assert
    assert matched is True


def test_mark_dispatch_reacted_returns_false_on_unknown_id(pg_schema: str):
    # Arrange — a reaction lands for a dispatch this sender never
    # minted (out-of-order replay, wrong sender, stale ledger). The
    # return value is the audit signal.
    from scitex_agent_container._state.dispatch_ledger import (
        mark_dispatch_reacted,
    )

    # Act
    matched = mark_dispatch_reacted("nonexistent-dispatch-id")
    # Assert
    assert matched is False


def test_mark_dispatch_reacted_is_idempotent(pg_schema: str):
    # Arrange — a duplicate receipt (network retry) must not corrupt
    # the row. The second call still writes REACTED on REACTED.
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_REACTED,
        list_dispatches,
        mark_dispatch_reacted,
        record_dispatch,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    mark_dispatch_reacted(did)
    # Act
    mark_dispatch_reacted(did)
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_REACTED


# ---------------------------------------------------------------------------
# list_unreacted_dispatches — the comm-miss surface.
# ---------------------------------------------------------------------------


def test_list_unreacted_includes_old_sent_rows(pg_schema: str):
    # Arrange — a dispatch minted 60s ago, never REACTED.
    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    record_dispatch(
        from_agent="a",
        to_agent="b",
        text="hi",
        ts=time.time() - 60.0,
    )
    # Act — SLO is 30s, so the 60s-old row IS overdue.
    rows = list_unreacted_dispatches(older_than_s=30.0)
    # Assert
    assert len(rows) == 1


def test_list_unreacted_excludes_fresh_rows_under_slo(pg_schema: str):
    # Arrange — a dispatch minted 5s ago is NOT a miss; the receiver
    # has not had time to react yet.
    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    record_dispatch(
        from_agent="a",
        to_agent="b",
        text="hi",
        ts=time.time() - 5.0,
    )
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0)
    # Assert
    assert rows == []


def test_list_unreacted_excludes_reacted_rows(pg_schema: str):
    # Arrange — REACTED is success; it must never appear as a miss.
    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        mark_dispatch_reacted,
        record_dispatch,
    )

    did = record_dispatch(
        from_agent="a",
        to_agent="b",
        text="hi",
        ts=time.time() - 60.0,
    )
    mark_dispatch_reacted(did)
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0)
    # Assert
    assert rows == []


def test_list_unreacted_excludes_failed_rows(pg_schema: str):
    # Arrange — failed rows are ALREADY known not to have landed;
    # surfacing them in comm-miss is noise.
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_FAILED,
        list_unreacted_dispatches,
        record_dispatch,
    )

    record_dispatch(
        from_agent="a",
        to_agent="b",
        text="hi",
        status=STATUS_FAILED,
        ts=time.time() - 60.0,
    )
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0)
    # Assert
    assert rows == []


def test_list_unreacted_narrows_by_to_agent(pg_schema: str):
    # Arrange — two stale rows, one to bob, one to carol. Filter to
    # bob only.
    from scitex_agent_container._state.dispatch_ledger import (
        list_unreacted_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="bob", text="hi", ts=time.time() - 60.0)
    record_dispatch(from_agent="a", to_agent="carol", text="hi", ts=time.time() - 60.0)
    # Act
    rows = list_unreacted_dispatches(older_than_s=30.0, to_agent="bob")
    # Assert
    assert len(rows) == 1
