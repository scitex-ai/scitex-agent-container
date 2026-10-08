"""Tests for the periodic-drive CCT injection port.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks; real
envelopes + a list-appender for the emit side-effect.
"""

from __future__ import annotations

from scitex_agent_container._delivery._redeliver import RedeliveryPolicy
from scitex_agent_container._delivery._redeliver import (
    redeliver as _redeliver,
)
from scitex_agent_container._delivery._state import DeliveryState
from scitex_agent_container._lifecycle._periodic_drive import (
    ENVELOPE_KIND,
    PeriodicDriveEnvelope,
)
from scitex_agent_container._lifecycle._periodic_drive_port import (
    CCT_DRIVE_KIND,
    build_cct_delivery_turn,
    inject_cct_turn,
)


def _delivered(agent: str = "peer") -> DeliveryState:
    state = DeliveryState(agent=agent, strategy="tui")
    for name in (
        "is_route_resolved",
        "is_payload_delivered",
        "is_payload_submitted",
    ):
        state = state.with_signal(name, True, f"{name}=True")
    return state


def test_cct_kind_is_distinct_from_periodic_drive_kind():
    # Arrange
    # Act
    # Assert
    assert CCT_DRIVE_KIND != ENVELOPE_KIND


def test_built_turn_carries_cct_kind():
    # Arrange
    summary = "agent=peer delivered=True attempts=1"
    # Act
    turn = build_cct_delivery_turn("peer", summary, now=1000.0)
    # Assert
    assert turn.kind == CCT_DRIVE_KIND


def test_built_turn_body_contains_agent_name():
    # Arrange
    summary = "agent=peer delivered=True attempts=1"
    # Act
    turn = build_cct_delivery_turn("peer", summary, now=1000.0)
    # Assert
    assert "peer" in turn.body


def test_built_turn_body_carries_summary_verbatim():
    # Arrange
    summary = "agent=peer delivered=False attempts=3 actions=retried,escalated"
    # Act
    turn = build_cct_delivery_turn("peer", summary, now=1000.0)
    # Assert
    assert summary in turn.body


def test_built_turn_honours_now():
    # Arrange
    summary = "summary"
    # Act
    turn = build_cct_delivery_turn("peer", summary, now=1234.0)
    # Assert
    assert turn.generated_at == 1234.0


def test_inject_emits_exactly_one_envelope():
    # Arrange
    emitted: list[PeriodicDriveEnvelope] = []
    # Act
    inject_cct_turn("peer", "summary", emit=emitted.append, now=1000.0)
    # Assert
    assert len(emitted) == 1


def test_inject_returns_the_emitted_envelope():
    # Arrange
    emitted: list[PeriodicDriveEnvelope] = []
    # Act
    turn = inject_cct_turn("peer", "summary", emit=emitted.append, now=1.0)
    # Assert
    assert emitted[0] == turn


def test_redelivery_summary_flows_into_turn_body():
    # Arrange
    def sender(agent: str, message: str, *, strategy: str = "auto"):
        return _delivered(agent)

    emitted: list[PeriodicDriveEnvelope] = []
    # Act
    report = _redeliver("peer", "hi", strategy="tui", deliver_fn=sender)
    inject_cct_turn(
        "peer", report.summary_text(), emit=emitted.append, now=1000.0
    )
    # Assert
    assert "agent=peer delivered=True" in emitted[0].body


def test_inject_uses_same_emit_shape_as_sweep():
    # Arrange
    from scitex_agent_container._lifecycle._periodic_drive import (
        _AgentState,
        sweep,
    )

    via_sweep: list[PeriodicDriveEnvelope] = []
    via_port: list[PeriodicDriveEnvelope] = []
    state = _AgentState(
        name="peer",
        is_running=True,
        workdir="/w",
        branch="b",
        last_commit_subject="c",
        worktree_name="wt",
        standing_rules="r",
        mission="m",
        last_drive_at=0.0,
        interval_s=60.0,
        enabled=True,
    )
    # Act
    sweep([state], emit=via_sweep.append, now=5000.0)
    inject_cct_turn("peer", "s", emit=via_port.append, now=5000.0)
    # Assert
    assert type(via_port[0]) is type(via_sweep[0])


def test_failed_redelivery_summary_carries_remedy_into_turn():
    # Arrange
    def sender(agent: str, message: str, *, strategy: str = "auto"):
        state = DeliveryState(agent=agent, strategy="tui")
        return state  # all signals None -> UNKNOWN -> no resend

    emitted: list[PeriodicDriveEnvelope] = []
    # Act
    report = _redeliver(
        "peer",
        "hi",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=3),
        deliver_fn=sender,
    )
    inject_cct_turn(
        "peer", report.summary_text(), emit=emitted.append, now=1000.0
    )
    # Assert
    assert "do NOT resend" in emitted[0].body
