"""Tests for the agent-list AUTH status — green vs green-AND-actually-working.

The operator's complaint, verbatim: an agent can be green ``running`` because
tmux is up, while it is not actually operational. These tests pin the behaviour
that fixes it, and — just as importantly — the two ways that fix could
accidentally make things WORSE:

* an ``auth-failed`` row must stay in the LIVE set, or the default view would
  HIDE the one status the operator asked to see;
* it must stay in the LIVE set for ``restart --all-running`` too, or the sweep
  would skip exactly the agents a restart would cure.

No mocks: pure functions over real dicts, plus a real state.db in ``tmp_path``
for the end-to-end read.

TQ: AAA marker triple (TQ002), one asserted fact per test (TQ007).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._helpers import _agent_list_auth as ala

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def now() -> datetime:
    """A fixed reference instant so age/staleness assertions are deterministic."""
    return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    """``moment`` in the exact ISO-8601 UTC 'Z' shape the store writes."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def failing(now: datetime) -> dict:
    """A cached verdict: this agent's auth is failing, checked 2 minutes ago."""
    return {
        "figrecipe": {
            "auth_failed": True,
            "checked_at": _stamp(now - timedelta(minutes=2)),
            "banner": "Login expired",
            "reason": "revoked",
        }
    }


# ---------------------------------------------------------------------------
# is_live_status — the predicate both the view and the restart sweep depend on
# ---------------------------------------------------------------------------


def test_running_agent_is_live() -> None:
    # Arrange
    status = "running"
    # Act
    live = ala.is_live_status(status)
    # Assert
    assert live is True


def test_auth_failed_agent_is_still_live() -> None:
    # Arrange — tmux is up and the pane process is alive; the agent simply cannot
    # call the API. If this read False the default view would hide it (and
    # `restart --all-running` would skip it) — the two ways to make this worse.
    status = ala.STATUS_AUTH_FAILED
    # Act
    live = ala.is_live_status(status)
    # Assert
    assert live is True


def test_stopped_agent_is_not_live() -> None:
    # Arrange
    status = "stopped"
    # Act
    live = ala.is_live_status(status)
    # Assert
    assert live is False


def test_unknown_status_is_not_live() -> None:
    # Arrange
    status = None
    # Act
    live = ala.is_live_status(status)
    # Assert
    assert live is False


# ---------------------------------------------------------------------------
# resolve_auth — the status upgrade
# ---------------------------------------------------------------------------


def test_running_agent_with_a_failing_verdict_is_no_longer_reported_running(
    failing: dict, now: datetime
) -> None:
    # Arrange — the whole point: liveness says "running", the auth cache says the
    # agent cannot authenticate. Green would be a lie.
    # Act
    _fields, status = ala.resolve_auth("figrecipe", failing, None, "running")
    # Assert
    assert status == ala.STATUS_AUTH_FAILED


def test_running_agent_with_a_healthy_verdict_stays_running(now: datetime) -> None:
    # Arrange
    states = {"worker": {"auth_failed": False, "checked_at": _stamp(now)}}
    # Act
    _fields, status = ala.resolve_auth("worker", states, None, "running")
    # Assert
    assert status == "running"


def test_never_checked_agent_stays_running(now: datetime) -> None:
    # Arrange — no evidence is not evidence of failure; we must not invent an
    # alarm for an agent the watchdog has never looked at.
    # Act
    _fields, status = ala.resolve_auth("worker", {}, None, "running")
    # Assert
    assert status == "running"


def test_stopped_agent_is_never_relabelled_auth_failed(failing: dict) -> None:
    # Arrange — a stale failing verdict for an agent that is no longer running
    # would be a claim about a process that is not there. `figrecipe` has a
    # cached failure, but it is stopped.
    # Act
    _fields, status = ala.resolve_auth("figrecipe", failing, None, "stopped")
    # Assert
    assert status == "stopped"


def test_stopped_agent_carries_no_auth_claim(failing: dict) -> None:
    # Arrange — and its row must report no evidence at all, not a stale failure.
    # Act
    fields, _status = ala.resolve_auth("figrecipe", failing, None, "stopped")
    # Assert
    assert fields["auth_failed"] is False


def test_resolved_row_carries_the_age_of_its_evidence(
    failing: dict, now: datetime
) -> None:
    # Arrange — a 6h-old verdict is weaker evidence than a 60s-old one, so the
    # age of the check must reach the renderer. It is measured against the real
    # clock, so the field's presence is what we pin, not an exact second count.
    # Act
    fields, _status = ala.resolve_auth("figrecipe", failing, None, "running")
    # Assert
    assert fields["auth_check_age_s"] is not None


def test_resolved_row_carries_the_remedy_for_a_revoked_token(failing: dict) -> None:
    # Arrange — "revoked" means the on-disk credential is fine and only the
    # process is stale: restart, do NOT drag the operator into a re-login.
    # Act
    fields, _status = ala.resolve_auth("figrecipe", failing, None, "running")
    # Assert
    assert fields["auth_remedy"] == "restart"


def test_verdict_from_before_the_current_start_does_not_relabel_the_row(
    now: datetime,
) -> None:
    # Arrange — the operator already restarted this agent, which is the cure. A
    # verdict older than the current incarnation must not keep calling it broken.
    states = {
        "figrecipe": {
            "auth_failed": True,
            "checked_at": _stamp(now - timedelta(hours=2)),
        }
    }
    started_at = _stamp(now - timedelta(minutes=1))
    # Act
    _fields, status = ala.resolve_auth("figrecipe", states, started_at, "running")
    # Assert
    assert status == "running"


# ---------------------------------------------------------------------------
# all_auth_states — the ONE-query cache read, end to end against a real db
# ---------------------------------------------------------------------------


def test_cache_read_survives_a_host_with_no_state_db(tmp_path: Path) -> None:
    # Arrange — `sac agents list` must never crash (or stall) on an auth-cache
    # miss; a fresh host simply has nobody checked yet.
    from scitex_agent_container._state import auth_state as aus

    # Act
    states = aus.list_auth_states(db_path=tmp_path / "absent.db")
    # Assert
    assert states == {}


def test_watchdog_write_is_visible_to_the_list_read(tmp_path: Path) -> None:
    # Arrange — the real contract between the two halves of this feature: the
    # watchdog persists, the list reads. Real sqlite file, real row, no mocks.
    from scitex_agent_container._state import auth_state as aus

    db_path = tmp_path / "state.db"
    aus.record_auth_check("figrecipe", True, reason="revoked", db_path=db_path)
    # Act
    states = aus.list_auth_states(db_path=db_path)
    # Assert
    assert states["figrecipe"]["auth_failed"] is True
