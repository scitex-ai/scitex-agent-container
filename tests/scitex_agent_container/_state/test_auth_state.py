"""Tests for the cached per-agent auth verdict (``agent_auth_state``).

The table that lets ``sac agents list`` say "this agent is tmux-GREEN but cannot
call the API" without probing auth inline. Two halves are covered:

* the STORE — a real state.db in ``tmp_path``, real rows, real sqlite (no mocks,
  no monkeypatch): write via the watchdog's ``record_auth_checks``, read back via
  the list's ``list_auth_states``;
* the HONESTY RULES in :func:`verdict_for` — pure, so they are exercised with
  plain dicts and an injected ``now``: a verdict has an AGE (stale evidence is
  flagged, never asserted as fresh) and a SCOPE (a verdict older than the agent's
  current ``started_at`` describes a dead incarnation and is discarded).

TQ: AAA marker triple (TQ002), one asserted fact per test (TQ007),
behaviour-naming (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scitex_agent_container._state import auth_state as aus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A per-test state.db path; the writer creates the schema on demand."""
    return tmp_path / "state.db"


@pytest.fixture
def now() -> datetime:
    """A fixed reference instant, so age/staleness assertions are deterministic."""
    return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    """``moment`` in the exact ISO-8601 UTC 'Z' shape the store writes."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# record_auth_checks / list_auth_states — the store round-trip
# ---------------------------------------------------------------------------


def test_recorded_failing_agent_reads_back_as_auth_failed(db: Path) -> None:
    # Arrange
    aus.record_auth_check("figrecipe", True, banner="Login expired", db_path=db)
    # Act
    state = aus.list_auth_states(db_path=db)["figrecipe"]
    # Assert
    assert state["auth_failed"] is True


def test_recorded_healthy_agent_reads_back_as_not_failed(db: Path) -> None:
    # Arrange
    aus.record_auth_check("worker", False, db_path=db)
    # Act
    state = aus.list_auth_states(db_path=db)["worker"]
    # Assert
    assert state["auth_failed"] is False


def test_recorded_check_carries_the_diagnosed_reason(db: Path) -> None:
    # Arrange
    aus.record_auth_check("figrecipe", True, reason="revoked", db_path=db)
    # Act
    state = aus.list_auth_states(db_path=db)["figrecipe"]
    # Assert
    assert state["reason"] == "revoked"


def test_recorded_check_stamps_checked_at(db: Path) -> None:
    # Arrange
    aus.record_auth_check("worker", False, checked_at="2026-07-13T11:59:00Z", db_path=db)
    # Act
    state = aus.list_auth_states(db_path=db)["worker"]
    # Assert
    assert state["checked_at"] == "2026-07-13T11:59:00Z"


def test_re_recording_an_agent_overwrites_rather_than_duplicating(db: Path) -> None:
    # Arrange — the watchdog re-runs and the agent has recovered.
    aus.record_auth_check("figrecipe", True, db_path=db)
    aus.record_auth_check("figrecipe", False, db_path=db)
    # Act
    state = aus.list_auth_states(db_path=db)["figrecipe"]
    # Assert
    assert state["auth_failed"] is False


def test_batch_write_records_every_named_agent(db: Path) -> None:
    # Arrange
    checks = [
        {"name": "a", "auth_failed": False},
        {"name": "b", "auth_failed": True},
    ]
    aus.record_auth_checks(checks, db_path=db)
    # Act
    names = sorted(aus.list_auth_states(db_path=db))
    # Assert
    assert names == ["a", "b"]


def test_clear_auth_state_removes_the_agents_verdict(db: Path) -> None:
    # Arrange
    aus.record_auth_check("figrecipe", True, db_path=db)
    aus.clear_auth_state("figrecipe", db_path=db)
    # Act
    states = aus.list_auth_states(db_path=db)
    # Assert
    assert "figrecipe" not in states


# ---------------------------------------------------------------------------
# list_auth_states — the CHEAP read: never create, never crash
# ---------------------------------------------------------------------------


def test_read_of_absent_db_returns_no_verdicts(tmp_path: Path) -> None:
    # Arrange — a host where no watchdog has ever run.
    missing = tmp_path / "never-created.db"
    # Act
    states = aus.list_auth_states(db_path=missing)
    # Assert
    assert states == {}


def test_read_of_absent_db_does_not_create_it(tmp_path: Path) -> None:
    # Arrange — the READ path must not materialise a state.db (nor take a
    # write lock) just because an operator typed `sac agents list`.
    missing = tmp_path / "never-created.db"
    # Act
    aus.list_auth_states(db_path=missing)
    # Assert
    assert missing.exists() is False


def test_read_of_db_without_the_table_returns_no_verdicts(tmp_path: Path) -> None:
    # Arrange — a real, pre-existing state.db that no watchdog has written to,
    # so `agent_auth_state` does not exist yet.
    from scitex_agent_container._state.state_db import init_schema

    db_path = tmp_path / "state.db"
    init_schema(db_path)
    # Act
    states = aus.list_auth_states(db_path=db_path)
    # Assert
    assert states == {}


# ---------------------------------------------------------------------------
# Staleness — a cache is not truth, it has an age
# ---------------------------------------------------------------------------


def test_fresh_verdict_is_not_stale(now: datetime) -> None:
    # Arrange — checked 60 seconds ago.
    checked = _stamp(now - timedelta(seconds=60))
    # Act
    stale = aus.is_stale(checked, now=now)
    # Assert
    assert stale is False


def test_six_hour_old_verdict_is_stale(now: datetime) -> None:
    # Arrange — the evidence the operator must not be shown as fresh truth.
    checked = _stamp(now - timedelta(hours=6))
    # Act
    stale = aus.is_stale(checked, now=now)
    # Assert
    assert stale is True


def test_never_checked_is_not_reported_as_stale(now: datetime) -> None:
    # Arrange — no stamp at all is NO EVIDENCE, a different state from old
    # evidence; readers discriminate on the empty checked_at, not on this flag.
    # Act
    stale = aus.is_stale("", now=now)
    # Assert
    assert stale is False


def test_age_of_a_future_stamp_clamps_to_zero(now: datetime) -> None:
    # Arrange — clock skew between the watchdog host and the reader.
    checked = _stamp(now + timedelta(minutes=5))
    # Act
    age = aus.age_seconds(checked, now=now)
    # Assert
    assert age == 0.0


# ---------------------------------------------------------------------------
# verdict_for — the pure read-side rule
# ---------------------------------------------------------------------------


def test_verdict_for_reports_a_current_failure(now: datetime) -> None:
    # Arrange
    state = {"auth_failed": True, "checked_at": _stamp(now), "reason": "revoked"}
    # Act
    fields = aus.verdict_for(state, started_at=_stamp(now - timedelta(hours=1)), now=now)
    # Assert
    assert fields["auth_failed"] is True


def test_verdict_for_exposes_the_age_of_the_evidence(now: datetime) -> None:
    # Arrange
    state = {"auth_failed": True, "checked_at": _stamp(now - timedelta(minutes=2))}
    # Act
    fields = aus.verdict_for(state, now=now)
    # Assert
    assert fields["auth_check_age_s"] == 120


def test_verdict_for_flags_an_old_failure_as_stale(now: datetime) -> None:
    # Arrange — still shown (nothing self-heals) but marked as weak evidence.
    state = {"auth_failed": True, "checked_at": _stamp(now - timedelta(hours=6))}
    # Act
    fields = aus.verdict_for(state, now=now)
    # Assert
    assert fields["auth_check_stale"] is True


def test_verdict_for_derives_the_remedy_from_the_reason(now: datetime) -> None:
    # Arrange — a REVOKED token is cured by a restart, not by logging in.
    state = {"auth_failed": True, "checked_at": _stamp(now), "reason": "revoked"}
    # Act
    fields = aus.verdict_for(state, now=now)
    # Assert
    assert fields["auth_remedy"] == "restart"


def test_verdict_for_prescribes_login_for_a_genuinely_expired_token(
    now: datetime,
) -> None:
    # Arrange
    state = {"auth_failed": True, "checked_at": _stamp(now), "reason": "expired"}
    # Act
    fields = aus.verdict_for(state, now=now)
    # Assert
    assert fields["auth_remedy"] == "login"


def test_verdict_predating_the_current_start_is_discarded(now: datetime) -> None:
    # Arrange — the operator restarted the wedged agent 1 minute ago; the
    # watchdog's older FAILING verdict describes the incarnation that is gone.
    # Replaying it would call the agent broken at the exact moment it was fixed.
    state = {"auth_failed": True, "checked_at": _stamp(now - timedelta(hours=2))}
    started_at = _stamp(now - timedelta(minutes=1))
    # Act
    fields = aus.verdict_for(state, started_at=started_at, now=now)
    # Assert
    assert fields["auth_failed"] is False


def test_verdict_predating_the_current_start_reports_no_evidence(
    now: datetime,
) -> None:
    # Arrange — and it must read as NEVER-CHECKED (no evidence), not as a
    # healthy check, so the reader says "unverified" rather than "fine".
    state = {"auth_failed": True, "checked_at": _stamp(now - timedelta(hours=2))}
    started_at = _stamp(now - timedelta(minutes=1))
    # Act
    fields = aus.verdict_for(state, started_at=started_at, now=now)
    # Assert
    assert fields["auth_checked_at"] == ""


def test_verdict_taken_after_the_current_start_is_kept(now: datetime) -> None:
    # Arrange — checked AFTER this incarnation booted ⇒ it describes this
    # process, and a still-wedged agent must keep reading as failing.
    state = {"auth_failed": True, "checked_at": _stamp(now - timedelta(minutes=1))}
    started_at = _stamp(now - timedelta(hours=2))
    # Act
    fields = aus.verdict_for(state, started_at=started_at, now=now)
    # Assert
    assert fields["auth_failed"] is True


def test_verdict_for_never_checked_agent_makes_no_claim() -> None:
    # Arrange — no cached row at all.
    # Act
    fields = aus.verdict_for(None)
    # Assert
    assert fields["auth_checked_at"] == ""


def test_verdict_for_never_checked_agent_is_not_called_failing() -> None:
    # Arrange — absence of evidence is not evidence of failure.
    # Act
    fields = aus.verdict_for(None)
    # Assert
    assert fields["auth_failed"] is False


def test_stored_verdict_survives_the_full_write_read_verdict_path(
    db: Path, now: datetime
) -> None:
    # Arrange — the real end-to-end shape: watchdog writes, list reads, rule
    # applies. No mocks anywhere: a real sqlite file, a real row.
    aus.record_auth_checks(
        [{"name": "figrecipe", "auth_failed": True, "reason": "revoked"}],
        checked_at=_stamp(now - timedelta(minutes=3)),
        db_path=db,
    )
    states = aus.list_auth_states(db_path=db)
    # Act
    fields = aus.verdict_for(states.get("figrecipe"), now=now)
    # Assert
    assert (fields["auth_failed"], fields["auth_remedy"]) == (True, "restart")
