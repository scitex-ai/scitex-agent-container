"""Tests for the CI-verdict delivery dedup table (sac #404).

The CI-feedback ring (feedback.pdf §3) requires sac to deliver each
GitHub CI verdict to the pusher EXACTLY ONCE, even though it polls
GitHub repeatedly. Dedup key is ``(repo, pr, head_sha, conclusion)`` —
sac's own ``delivered-set`` (the PDF's term), kept in ``state.db`` so it
survives a ``sac listen`` restart.

Conventions (mirroring test_dispatch_ledger.py):

  * One assertion per test (STX-TQ007); related invariants via
    ``pytest.parametrize``.
  * AAA markers (Arrange / Act / Assert).
  * No mocks / monkeypatch (STX-NM); real sqlite under ``tmp_path``,
    isolated via the ``db_path`` env fixture (explicit save/restore).
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env so callers pick it up.

    Explicit env save/restore (no monkeypatch fixture, PA-306).
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_init_schema_creates_verdict_delivered_table(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_verdict_dedup import (
        init_verdict_dedup_schema,
    )

    # Act
    init_verdict_dedup_schema()
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Assert
    assert "verdict_delivered" in names


def test_record_creates_table_lazily_without_explicit_init(db_path: Path):
    # Arrange — never call init; record must ensure the table itself.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
    )

    # Act
    record_verdict_delivered(
        repo="ywatanabe1989/scitex-dev", pr=1, head_sha="abc", conclusion="success"
    )
    # Assert
    assert verdict_already_delivered(
        repo="ywatanabe1989/scitex-dev", pr=1, head_sha="abc", conclusion="success"
    )


# ---------------------------------------------------------------------------
# Dedup semantics
# ---------------------------------------------------------------------------


def test_fresh_key_is_not_delivered(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_verdict_dedup import (
        verdict_already_delivered,
    )

    # Act
    seen = verdict_already_delivered(
        repo="ywatanabe1989/scitex-dev", pr=7, head_sha="deadbeef", conclusion="success"
    )
    # Assert
    assert seen is False


def test_recorded_key_is_delivered(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
    )

    # Act
    record_verdict_delivered(
        repo="ywatanabe1989/scitex-dev", pr=7, head_sha="deadbeef", conclusion="failure"
    )
    # Assert
    assert verdict_already_delivered(
        repo="ywatanabe1989/scitex-dev", pr=7, head_sha="deadbeef", conclusion="failure"
    )


def test_different_conclusion_is_a_distinct_key(db_path: Path):
    # Arrange — a re-run flipping red→green is a NEW verdict to deliver.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
    )

    record_verdict_delivered(repo="r", pr=3, head_sha="sha1", conclusion="failure")
    # Act
    seen_success = verdict_already_delivered(
        repo="r", pr=3, head_sha="sha1", conclusion="success"
    )
    # Assert
    assert seen_success is False


def test_different_head_sha_is_a_distinct_key(db_path: Path):
    # Arrange — a new push (new head_sha) is a new verdict.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
    )

    record_verdict_delivered(repo="r", pr=3, head_sha="sha1", conclusion="success")
    # Act
    seen_other = verdict_already_delivered(
        repo="r", pr=3, head_sha="sha2", conclusion="success"
    )
    # Assert
    assert seen_other is False


def test_record_is_idempotent(db_path: Path):
    # Arrange — re-seeing the same verdict (re-poll) must not raise.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
    )

    record_verdict_delivered(repo="r", pr=9, head_sha="s", conclusion="success")
    # Act
    record_verdict_delivered(repo="r", pr=9, head_sha="s", conclusion="success")
    # Assert
    assert verdict_already_delivered(repo="r", pr=9, head_sha="s", conclusion="success")


# ---------------------------------------------------------------------------
# Failure streak — the consecutive-failure cap's counter
#
# `delivered_at` is passed explicitly throughout: the streak is defined by
# comparison against the last green's timestamp, and two records written in
# the same float tick would make the ordering ambiguous. Production ticks are
# minutes apart, so this is a test-determinism concern, not a live one.
# ---------------------------------------------------------------------------


def test_failure_streak_counts_reds_for_this_pr(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db_verdict_dedup import (
        failures_since_last_success,
        record_verdict_delivered,
    )

    for i, sha in enumerate(("a", "b", "c")):
        record_verdict_delivered(
            repo="o/r", pr=1, head_sha=sha, conclusion="failure", delivered_at=100.0 + i
        )
    # Act
    streak = failures_since_last_success(repo="o/r", pr=1)
    # Assert
    assert streak == 3


def test_failure_streak_resets_after_a_green(db_path: Path):
    # Arrange — two reds, a green, then one more red.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        failures_since_last_success,
        record_verdict_delivered,
    )

    record_verdict_delivered(
        repo="o/r", pr=1, head_sha="a", conclusion="failure", delivered_at=100.0
    )
    record_verdict_delivered(
        repo="o/r", pr=1, head_sha="b", conclusion="failure", delivered_at=101.0
    )
    record_verdict_delivered(
        repo="o/r", pr=1, head_sha="g", conclusion="success", delivered_at=102.0
    )
    record_verdict_delivered(
        repo="o/r", pr=1, head_sha="d", conclusion="failure", delivered_at=103.0
    )
    # Act
    streak = failures_since_last_success(repo="o/r", pr=1)
    # Assert
    assert streak == 1


def test_failure_streak_ignores_another_prs_reds(db_path: Path):
    # Arrange — a noisy neighbour must not cap this PR.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        failures_since_last_success,
        record_verdict_delivered,
    )

    for i, sha in enumerate(("a", "b", "c", "d", "e")):
        record_verdict_delivered(
            repo="o/r",
            pr=999,
            head_sha=sha,
            conclusion="failure",
            delivered_at=100.0 + i,
        )
    # Act
    streak = failures_since_last_success(repo="o/r", pr=1)
    # Assert
    assert streak == 0


def test_failure_streak_ignores_another_repos_reds(db_path: Path):
    # Arrange — same PR number in a different repo is a different PR.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        failures_since_last_success,
        record_verdict_delivered,
    )

    for i, sha in enumerate(("a", "b", "c", "d")):
        record_verdict_delivered(
            repo="other/repo",
            pr=1,
            head_sha=sha,
            conclusion="failure",
            delivered_at=100.0 + i,
        )
    # Act
    streak = failures_since_last_success(repo="o/r", pr=1)
    # Assert
    assert streak == 0


def test_failure_streak_is_zero_on_a_fresh_db(db_path: Path):
    # Arrange — never written; the table must be ensured lazily, not raise.
    from scitex_agent_container._state.state_db_verdict_dedup import (
        failures_since_last_success,
    )

    # Act
    streak = failures_since_last_success(repo="o/r", pr=1)
    # Assert
    assert streak == 0
