"""Persistent deduplicated agentic-ACK nudge state machine tests."""

from __future__ import annotations


class _Repo:
    def __init__(self) -> None:
        self.rows = {}

    def get(self, agent: str, dispatch_id: str):
        return self.rows.get((agent, dispatch_id))

    def put(self, record) -> None:
        self.rows[(record.agent, record.dispatch_id)] = record

    def pending(self, agent: str):
        return [
            row
            for (owner, _), row in self.rows.items()
            if owner == agent and row.state == "pending"
        ]


def _api():
    from scitex_agent_container._state.dispatch_nudges import (
        schedule_nudge,
        tick_nudges,
    )

    return schedule_nudge, tick_nudges


def test_repeated_ticks_honor_bounded_exponential_backoff() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    sent: list[str] = []
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=1000.0,
    )
    # Act
    for now in (9.0, 10.0, 15.0, 30.0, 69.0, 70.0):
        tick_nudges(
            repo,
            agent="alice",
            now=now,
            status_of=lambda _: "delivered",
            send_nudge=lambda row: sent.append(row.dispatch_id),
            escalate=lambda row: None,
            initial_delay_s=10.0,
            max_delay_s=40.0,
        )
    row = repo.rows[("alice", "nonce")]
    # Assert
    assert (sent, row.attempts, row.next_nudge_at) == (["nonce"] * 3, 3, 110.0)


def test_restart_dedup_does_not_repeat_same_nudge_window() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    sent: list[str] = []
    kwargs = {
        "agent": "alice",
        "dispatch_id": "nonce",
        "target": "bob",
        "now": 0.0,
        "initial_delay_s": 10.0,
        "deadline_s": 1000.0,
    }
    schedule_nudge(repo, **kwargs)
    # Act — a restarted process schedules again and ticks the same instant.
    schedule_nudge(repo, **kwargs)
    for _ in range(2):
        tick_nudges(
            repo,
            agent="alice",
            now=10.0,
            status_of=lambda _: "reacted",
            send_nudge=lambda row: sent.append(row.dispatch_id),
            escalate=lambda row: None,
            initial_delay_s=10.0,
            max_delay_s=40.0,
        )
    # Assert
    assert sent == ["nonce"]


def test_tick_stops_immediately_on_agentic_ack() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    sent: list[str] = []
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=1000.0,
    )
    # Act
    tick_nudges(
        repo,
        agent="alice",
        now=10.0,
        status_of=lambda _: "agentic_acked",
        send_nudge=lambda row: sent.append(row.dispatch_id),
        escalate=lambda row: None,
        initial_delay_s=10.0,
        max_delay_s=40.0,
    )
    row = repo.rows[("alice", "nonce")]
    # Assert
    assert (sent, row.state) == ([], "stopped")


def test_wrong_nonce_ack_does_not_stop_real_dispatch() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    sent: list[str] = []
    statuses = {"real-nonce": "reacted", "wrong-nonce": "agentic_acked"}
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="real-nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=1000.0,
    )
    # Act
    tick_nudges(
        repo,
        agent="alice",
        now=10.0,
        status_of=lambda nonce: statuses[nonce],
        send_nudge=lambda row: sent.append(row.dispatch_id),
        escalate=lambda row: None,
        initial_delay_s=10.0,
        max_delay_s=40.0,
    )
    # Assert
    assert sent == ["real-nonce"]


def test_deadline_escalates_once_without_infinite_nudges() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    sent: list[str] = []
    escalated: list[str] = []
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=25.0,
    )
    # Act
    for now in (10.0, 25.0, 50.0):
        tick_nudges(
            repo,
            agent="alice",
            now=now,
            status_of=lambda _: "delivered",
            send_nudge=lambda row: sent.append(row.dispatch_id),
            escalate=lambda row: escalated.append(row.dispatch_id),
            initial_delay_s=10.0,
            max_delay_s=40.0,
        )
    row = repo.rows[("alice", "nonce")]
    # Assert
    assert (sent, escalated, row.state) == (["nonce"], ["nonce"], "escalated")


def test_three_consecutive_transport_failures_escalate_unreachable() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    escalated: list[str] = []
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=1000.0,
    )

    # Act
    for now in (10.0, 30.0, 70.0):
        tick_nudges(
            repo,
            agent="alice",
            now=now,
            status_of=lambda _: "delivered",
            send_nudge=lambda _row: False,
            escalate=lambda row: escalated.append(row.dispatch_id),
            initial_delay_s=10.0,
            max_delay_s=40.0,
            max_transport_failures=3,
        )
    row = repo.rows[("alice", "nonce")]

    # Assert
    assert (
        row.attempts,
        row.consecutive_transport_failures,
        row.state,
        escalated,
    ) == (3, 3, "transport_unreachable", ["nonce"])


def test_successful_transport_resets_consecutive_failure_count() -> None:
    # Arrange
    schedule_nudge, tick_nudges = _api()
    repo = _Repo()
    outcomes = iter((False, True))
    schedule_nudge(
        repo,
        agent="alice",
        dispatch_id="nonce",
        target="bob",
        now=0.0,
        initial_delay_s=10.0,
        deadline_s=1000.0,
    )

    # Act
    for now in (10.0, 30.0):
        tick_nudges(
            repo,
            agent="alice",
            now=now,
            status_of=lambda _: "delivered",
            send_nudge=lambda _row: next(outcomes),
            escalate=lambda _row: None,
            initial_delay_s=10.0,
            max_delay_s=40.0,
            max_transport_failures=3,
        )
    row = repo.rows[("alice", "nonce")]

    # Assert
    assert (row.attempts, row.consecutive_transport_failures, row.state) == (
        2,
        0,
        "pending",
    )
