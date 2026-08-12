"""A row naming another host is a RECORD, not a writer — until somebody looks at that host.

Written against the exact state the 2026-08-11 canary's return leg hit, because
the whole design follows from it. The lease is written to the COORDINATOR's own
state db and the coordinator is always the host being LEFT, so after A -> B the
row on A reads ``holder=B`` and B's store never hears about it. When B moves
back, the coordinator stands on B and reads a row from an EARLIER move:

    ywata-note-win state db:    holder=scitex-compute-04  fence 1
    scitex-compute-04 state db: holder=ywata-note-win     fence 1

Both are true. Neither is current. The old rule read either as a live second
writer and refused — at ``handover``, five phases after the agent was stopped.

So the property pinned here is not "does it refuse" but WHAT IT REFUSES ON: an
observation of the named host, three-valued, where "nobody looked" refuses as
firmly as "it is running" and says something different.

Pure predicates over real values. No mocks, no clock, no store.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_lease import (
    CODE_HELD_BY_OTHER,
    CODE_OK,
    CODE_UNKNOWN,
    Lease,
    claim,
)
from scitex_agent_container._lifecycle._relocate_lease_readiness import (
    handoff_readiness,
)

AGENT = "canary-resume-test"
A = "scitex-compute-04"
B = "ywata-note-win"
NOW = 1_786_500_000.0
TTL = 86_400.0


def _lease(holder: str, *, fence: int = 1, expires_at: float = NOW + TTL) -> Lease:
    return Lease(
        agent=AGENT, holder=holder, token="tok", expires_at=expires_at, fence=fence
    )


@pytest.fixture
def stale_row() -> Lease:
    """The canary's input: a LIVE row naming the other host, written yesterday."""
    return _lease(A, fence=1)


# ---------------------------------------------------------------------------
# the branches that proceed without needing anybody observed
# ---------------------------------------------------------------------------


def test_no_row_at_all_is_a_go() -> None:
    # Arrange: sac claims no lease when an agent starts, so a first move finds none.
    lease = None
    # Act
    verdict = handoff_readiness(lease, from_holder=A, now=NOW)
    # Assert
    assert verdict.allowed is True


def test_no_row_says_the_lease_will_be_bootstrapped() -> None:
    # Arrange
    lease = None
    # Act
    verdict = handoff_readiness(lease, from_holder=A, now=NOW)
    # Assert
    assert "BOOTSTRAPPED" in verdict.reason


def test_the_source_already_holding_it_is_a_go() -> None:
    # Arrange
    lease = _lease(A)
    # Act
    verdict = handoff_readiness(lease, from_holder=A, now=NOW)
    # Assert
    assert verdict.allowed is True


def test_an_expired_row_is_a_go_without_observing_anyone() -> None:
    # Arrange: the fence, not a probe, excludes a holder whose lease ran out.
    lease = _lease(B, expires_at=NOW - 1.0)
    # Act
    verdict = handoff_readiness(lease, from_holder=A, now=NOW)
    # Assert
    assert verdict.allowed is True


def test_an_expired_row_says_the_fence_will_advance() -> None:
    # Arrange
    lease = _lease(B, expires_at=NOW - 1.0)
    # Act
    verdict = handoff_readiness(lease, from_holder=A, now=NOW)
    # Assert
    assert "fence" in verdict.reason


# ---------------------------------------------------------------------------
# the live row naming somebody else — three answers, not two
# ---------------------------------------------------------------------------


def test_a_live_row_naming_another_host_is_UNKNOWN_when_unobserved(
    stale_row: Lease,
) -> None:
    # Arrange: exactly what the return leg read out of ywata-note-win's db.
    lease = stale_row
    # Act
    verdict = handoff_readiness(lease, from_holder=B, now=NOW)
    # Assert
    assert verdict.allowed is None


def test_the_unobserved_answer_carries_the_unknown_code(stale_row: Lease) -> None:
    # Arrange
    lease = stale_row
    # Act
    verdict = handoff_readiness(lease, from_holder=B, now=NOW)
    # Assert
    assert verdict.code == CODE_UNKNOWN


def test_the_unobserved_answer_names_the_host_to_go_and_look_at(
    stale_row: Lease,
) -> None:
    # Arrange
    lease = stale_row
    # Act
    verdict = handoff_readiness(lease, from_holder=B, now=NOW)
    # Assert
    assert A in verdict.reason


def test_a_holder_that_IS_running_the_agent_refuses(stale_row: Lease) -> None:
    # Arrange: this is the split-brain, now backed by an observation.
    lease = stale_row
    # Act
    verdict = handoff_readiness(
        lease, from_holder=B, now=NOW, recorded_holder_running=True
    )
    # Assert
    assert verdict.allowed is False


def test_a_running_holder_refuses_with_the_held_by_other_code(
    stale_row: Lease,
) -> None:
    # Arrange
    lease = stale_row
    # Act
    verdict = handoff_readiness(
        lease, from_holder=B, now=NOW, recorded_holder_running=True
    )
    # Assert
    assert verdict.code == CODE_HELD_BY_OTHER


def test_a_holder_that_is_NOT_running_the_agent_proceeds(stale_row: Lease) -> None:
    # Arrange: the 2026-08-11 return leg, measured.
    lease = stale_row
    # Act
    verdict = handoff_readiness(
        lease, from_holder=B, now=NOW, recorded_holder_running=False
    )
    # Assert
    assert verdict.allowed is True


def test_an_absent_holder_is_described_as_a_past_handover(stale_row: Lease) -> None:
    # Arrange
    lease = stale_row
    # Act
    verdict = handoff_readiness(
        lease, from_holder=B, now=NOW, recorded_holder_running=False
    )
    # Assert
    assert "past handover" in verdict.reason


def test_unobserved_and_refused_do_not_say_the_same_thing(stale_row: Lease) -> None:
    # Arrange: both stop the move; only one is about another host being alive.
    unobserved = handoff_readiness(stale_row, from_holder=B, now=NOW)
    # Act
    running = handoff_readiness(
        stale_row, from_holder=B, now=NOW, recorded_holder_running=True
    )
    # Assert
    assert unobserved.reason != running.reason


def test_an_unnamed_source_raises_rather_than_answering(stale_row: Lease) -> None:
    # Arrange
    lease = stale_row
    # Act / Assert are one statement for a raise.
    # Assert
    with pytest.raises(ValueError, match="naming the host"):
        handoff_readiness(lease, from_holder="", now=NOW)


# ---------------------------------------------------------------------------
# the gate and the primitive must agree, or the preflight is a lie
# ---------------------------------------------------------------------------


def test_the_go_it_reports_is_one_claim_will_actually_grant(stale_row: Lease) -> None:
    # Arrange: a "proceed" on a live row held elsewhere is worthless unless
    # claim() then takes it — with the same observation this verdict is made of.
    # (That the verdict IS a go on this input is pinned by
    # test_a_holder_that_is_NOT_running_the_agent_proceeds above.)
    # Act
    _granted, verdict = claim(
        stale_row,
        agent=AGENT,
        holder=B,
        token="new",
        now=NOW,
        ttl_s=TTL,
        holder_absent=True,
    )
    # Assert
    assert verdict.code == CODE_OK


def test_that_claim_advances_the_fence(stale_row: Lease) -> None:
    # Arrange: the fence is what actually locks the old holder out.
    starting_fence = stale_row.fence
    # Act
    granted, _verdict = claim(
        stale_row,
        agent=AGENT,
        holder=B,
        token="new",
        now=NOW,
        ttl_s=TTL,
        holder_absent=True,
    )
    # Assert
    assert granted.fence == starting_fence + 1


def test_claim_still_refuses_the_same_row_without_the_observation(
    stale_row: Lease,
) -> None:
    # Arrange: ``holder_absent`` is evidence, and absent evidence changes nothing.
    lease = stale_row
    # Act
    _granted, verdict = claim(
        lease, agent=AGENT, holder=B, token="new", now=NOW, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_HELD_BY_OTHER
