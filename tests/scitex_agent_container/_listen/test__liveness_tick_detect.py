"""Tests for the pure reconcile rule of the liveness-tick reconciler.

Card ``sac-card-anchored-stop-reconciler``. ``find_stuck_cards`` is pure
(no IO, no asyncio), so these tests feed it a REAL in-memory tasks doc +
REAL :class:`AgentLiveness` inputs and assert the stuck/progressing/
blocked/not-live branches. No mocks (STX-NM002).

STX-TQ002 AAA-markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container._listen._liveness_tick_detect import (
    AgentLiveness,
    find_stuck_cards,
    open_card_owners,
)

# A fixed "now" so staleness maths is deterministic.
NOW = 1_000_000.0
STALE_S = 900.0


def _iso(epoch: float) -> str:
    """ISO-8601 ``...Z`` string for an epoch second (the tasks.yaml shape)."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _card(**over) -> dict:
    """An OPEN card stale past the threshold, with an assignee + no blocker.

    Each test overrides only the field under test, so the baseline is the
    one that WOULD fire — keeping each case a single-variable change."""
    base = {
        "id": "card-x",
        "status": "in_progress",
        "assignee": "agent-x",
        "last_activity": _iso(NOW - STALE_S - 60.0),  # past threshold
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# owner not live → owner-not-live
# ---------------------------------------------------------------------------


class TestOwnerNotLive:
    def test_dead_owner_of_stale_open_card_is_an_anomaly(self) -> None:
        # Arrange — owner has no live registry row.
        doc = {"tasks": [_card()]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert [s.reason for s in stuck] == ["owner-not-live"]

    def test_owner_absent_from_liveness_map_is_not_live(self) -> None:
        # Arrange — no entry at all for the owner ⇒ treated as not live.
        doc = {"tasks": [_card()]}
        # Act
        stuck = find_stuck_cards(doc, {}, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck and stuck[0].reason == "owner-not-live"

    def test_anomaly_carries_the_card_id(self) -> None:
        # Arrange
        doc = {"tasks": [_card(id="card-77")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck[0].card_id == "card-77"


# ---------------------------------------------------------------------------
# owner live but session idle → owner-idle
# ---------------------------------------------------------------------------


class TestOwnerIdle:
    def test_live_owner_with_idle_session_is_an_anomaly(self) -> None:
        # Arrange — process alive, but session.jsonl last record is stale.
        doc = {"tasks": [_card()]}
        liveness = {
            "agent-x": AgentLiveness(
                is_live=True, last_active_ts=NOW - STALE_S - 30.0
            )
        }
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert [s.reason for s in stuck] == ["owner-idle"]

    def test_live_owner_with_unknown_session_is_idle(self) -> None:
        # Arrange — live but no session timestamp known (None) ⇒ idle.
        doc = {"tasks": [_card()]}
        liveness = {"agent-x": AgentLiveness(is_live=True, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck and stuck[0].reason == "owner-idle"


# ---------------------------------------------------------------------------
# progressing → no anomaly
# ---------------------------------------------------------------------------


class TestProgressingNoAnomaly:
    def test_live_owner_with_recent_session_is_progressing(self) -> None:
        # Arrange — live + session moved 1s ago ⇒ progressing.
        doc = {"tasks": [_card()]}
        liveness = {"agent-x": AgentLiveness(is_live=True, last_active_ts=NOW - 1.0)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_card_with_recent_last_activity_never_fires(self) -> None:
        # Arrange — card itself moved 1s ago; owner dead is irrelevant.
        doc = {"tasks": [_card(last_activity=_iso(NOW - 1.0))]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []


# ---------------------------------------------------------------------------
# declared blocker / parked status / terminal status → no anomaly
# ---------------------------------------------------------------------------


class TestSuppressedByDeclaration:
    def test_card_with_declared_blocker_is_suppressed(self) -> None:
        # Arrange — author declared WHY it's parked.
        doc = {"tasks": [_card(blocker="dependency")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_blank_blocker_string_does_not_suppress(self) -> None:
        # Arrange — empty/whitespace blocker is NOT a declared blocker.
        doc = {"tasks": [_card(blocker="   ")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck and stuck[0].reason == "owner-not-live"

    def test_blocked_status_is_a_parked_state(self) -> None:
        # Arrange — status=blocked is parked-on-purpose ⇒ not open.
        doc = {"tasks": [_card(status="blocked")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_deferred_status_is_a_parked_state(self) -> None:
        # Arrange
        doc = {"tasks": [_card(status="deferred")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_done_status_is_terminal(self) -> None:
        # Arrange — a resolved card is never open.
        doc = {"tasks": [_card(status="done")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []


# ---------------------------------------------------------------------------
# missing owner / id → skipped
# ---------------------------------------------------------------------------


class TestSkippedRows:
    def test_card_without_assignee_is_skipped(self) -> None:
        # Arrange — no owner to alarm against.
        card = _card()
        card.pop("assignee")
        doc = {"tasks": [card]}
        # Act
        stuck = find_stuck_cards(doc, {}, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_agent_key_is_honoured_when_assignee_absent(self) -> None:
        # Arrange — older cards use ``agent`` rather than ``assignee``.
        card = _card()
        card.pop("assignee")
        card["agent"] = "agent-legacy"
        doc = {"tasks": [card]}
        liveness = {"agent-legacy": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck and stuck[0].agent == "agent-legacy"

    def test_card_without_id_is_skipped(self) -> None:
        # Arrange — an id-less row can't be referenced downstream.
        doc = {"tasks": [_card(id="")]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck == []

    def test_non_dict_task_row_is_skipped(self) -> None:
        # Arrange — a malformed (non-dict) entry must not crash the rule.
        doc = {"tasks": ["not-a-dict", _card()]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert — the good row still fires; the bad row is ignored.
        assert len(stuck) == 1


# ---------------------------------------------------------------------------
# severity scaling
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_just_past_threshold_is_warning(self) -> None:
        # Arrange — stale by a little over one threshold.
        doc = {"tasks": [_card(last_activity=_iso(NOW - STALE_S - 10.0))]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck[0].severity == "warning"

    def test_far_past_threshold_is_critical(self) -> None:
        # Arrange — stale by > 4× the threshold.
        doc = {"tasks": [_card(last_activity=_iso(NOW - STALE_S * 5))]}
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        stuck = find_stuck_cards(doc, liveness, now=NOW, stale_s=STALE_S)
        # Assert
        assert stuck[0].severity == "critical"


# ---------------------------------------------------------------------------
# open_card_owners (the set the loop resolves liveness for)
# ---------------------------------------------------------------------------


class TestOpenCardOwners:
    def test_collects_only_open_unblocked_owners(self) -> None:
        # Arrange — one open+unblocked, one done, one blocked.
        doc = {
            "tasks": [
                _card(id="a", assignee="owner-open"),
                _card(id="b", status="done", assignee="owner-done"),
                _card(id="c", blocker="dep", assignee="owner-blocked"),
            ]
        }
        # Act
        owners = open_card_owners(doc)
        # Assert
        assert owners == {"owner-open"}


# EOF
