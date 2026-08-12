"""Two live writers for one agent must be UNREPRESENTABLE, not merely detected.

The 2026-08-07 split-brain: one identity, two hosts, two postgres stores,
neither seeing the other's writes. Nothing was broken — sac simply has no way to
say "only this instance may write", because `cardinality: singleton` is declared
in a spec and enforced by nothing.

These tests pin the two properties that make the lease worth having:

  * a second holder is REFUSED while the first lease is live (the lease), and
  * a holder that was superseded while paused is refused even if its clock says
    otherwise (the fence).

The fence is the one that catches the case a TTL cannot: a stopped container, a
suspended laptop or an NTP step can hand a source a lease it honestly believes
is valid. Arithmetic locks it out where clock comparison would not.

Pure functions, explicit `now`, no mocks and no sleeping.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_lease import (
    CODE_EXPIRED,
    CODE_HELD_BY_OTHER,
    CODE_NOT_HELD,
    CODE_OK,
    CODE_STALE_FENCE,
    CODE_UNKNOWN,
    CODE_WRONG_TOKEN,
    Lease,
    LeaseVerdict,
    check_write,
    claim,
    handoff,
    renew,
)

AGENT = "scitex-agent-container"
SRC = "ywata-note-win"
DST = "nas-03"
T0 = 1_000_000.0
TTL = 60.0


@pytest.fixture
def held() -> Lease:
    """A live lease held by the source at fence 0, valid T0 .. T0+TTL."""
    lease, _ = claim(None, agent=AGENT, holder=SRC, token="tok-src", now=T0, ttl_s=TTL)
    assert lease is not None  # harness guard, not the behaviour under test
    return lease


# ---------------------------------------------------------------------------
# claim — the refusal that prevents a second writer
# ---------------------------------------------------------------------------


def test_claim_grants_an_unheld_lease(held: Lease) -> None:
    # Arrange: the fixture claimed against no prior lease.
    lease = held
    # Act
    holder = lease.holder
    # Assert
    assert holder == SRC


def test_a_first_claim_starts_the_fence_at_zero(held: Lease) -> None:
    # Arrange
    lease = held
    # Act
    fence = lease.fence
    # Assert
    assert fence == 0


def test_claim_refuses_a_second_holder_while_the_lease_is_live(held: Lease) -> None:
    # Arrange — this is the copy-and-start case that made two writers.
    # Act
    _, verdict = claim(
        held, agent=AGENT, holder=DST, token="tok-dst", now=T0 + 1, ttl_s=TTL
    )
    # Assert
    assert verdict.allowed is False


def test_the_refused_second_holder_is_told_who_holds_it(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = claim(
        held, agent=AGENT, holder=DST, token="tok-dst", now=T0 + 1, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_HELD_BY_OTHER


def test_a_refused_claim_leaves_the_existing_lease_untouched(held: Lease) -> None:
    # Arrange
    # Act
    after, _ = claim(
        held, agent=AGENT, holder=DST, token="tok-dst", now=T0 + 1, ttl_s=TTL
    )
    # Assert
    assert after == held


def test_reclaiming_by_the_same_holder_is_idempotent(held: Lease) -> None:
    # Arrange — a retrying coordinator must not be punished for retrying.
    # Act
    _, verdict = claim(
        held, agent=AGENT, holder=SRC, token="tok-src", now=T0 + 1, ttl_s=TTL
    )
    # Assert
    assert verdict.allowed is True


def test_an_expired_lease_can_be_claimed_by_someone_else(held: Lease) -> None:
    # Arrange — past the deadline the previous holder has no authority.
    # Act
    _, verdict = claim(
        held, agent=AGENT, holder=DST, token="tok-dst", now=T0 + TTL + 1, ttl_s=TTL
    )
    # Assert
    assert verdict.allowed is True


def test_reclaiming_after_expiry_advances_the_fence(held: Lease) -> None:
    # Arrange — the previous holder may still be alive and unaware. The bumped
    # fence is what locks it out; without it, expiry alone trusts two clocks.
    # Act
    after, _ = claim(
        held, agent=AGENT, holder=DST, token="tok-dst", now=T0 + TTL + 1, ttl_s=TTL
    )
    # Assert
    assert after is not None and after.fence == held.fence + 1


def test_a_lease_for_a_different_agent_answers_unknown(held: Lease) -> None:
    # Arrange — the wrong record says nothing about this agent either way, and
    # "no" would be misread as "someone else holds it".
    # Act
    _, verdict = claim(
        held, agent="some-other-agent", holder=DST, token="t", now=T0, ttl_s=TTL
    )
    # Assert
    assert verdict.allowed is None


def test_a_lease_for_a_different_agent_is_coded_unknown(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = claim(
        held, agent="some-other-agent", holder=DST, token="t", now=T0, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_UNKNOWN


# ---------------------------------------------------------------------------
# expiry boundary
# ---------------------------------------------------------------------------


def test_a_lease_is_already_gone_at_its_deadline(held: Lease) -> None:
    # Arrange — a boundary favouring the holder would let two writers overlap
    # on one shared instant.
    # Act
    expired = held.is_expired(T0 + TTL)
    # Assert
    assert expired is True


def test_a_lease_is_live_just_before_its_deadline(held: Lease) -> None:
    # Arrange
    # Act
    expired = held.is_expired(T0 + TTL - 0.001)
    # Assert
    assert expired is False


# ---------------------------------------------------------------------------
# renew — extends, never resurrects
# ---------------------------------------------------------------------------


def test_renew_extends_the_deadline(held: Lease) -> None:
    # Arrange
    # Act
    after, _ = renew(held, holder=SRC, token="tok-src", fence=0, now=T0 + 10, ttl_s=TTL)
    # Assert
    assert after is not None and after.expires_at == T0 + 10 + TTL


def test_renew_keeps_the_fence_unchanged(held: Lease) -> None:
    # Arrange — renewing is not a change of authority, so nothing is superseded.
    # Act
    after, _ = renew(held, holder=SRC, token="tok-src", fence=0, now=T0 + 10, ttl_s=TTL)
    # Assert
    assert after is not None and after.fence == held.fence


def test_renew_refuses_a_different_holder(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = renew(
        held, holder=DST, token="tok-src", fence=0, now=T0 + 10, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_HELD_BY_OTHER


def test_renew_refuses_a_wrong_token(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = renew(
        held, holder=SRC, token="guessed", fence=0, now=T0 + 10, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_WRONG_TOKEN


def test_renew_refuses_a_stale_fence(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = renew(
        held, holder=SRC, token="tok-src", fence=99, now=T0 + 10, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_STALE_FENCE


def test_renew_never_resurrects_an_expired_lease(held: Lease) -> None:
    # Arrange — reviving it would hand a paused holder its OLD fence back,
    # which is exactly the writer the fence exists to exclude.
    # Act
    _, verdict = renew(
        held, holder=SRC, token="tok-src", fence=0, now=T0 + TTL + 1, ttl_s=TTL
    )
    # Assert
    assert verdict.code == CODE_EXPIRED


def test_renew_without_a_lease_reports_not_held() -> None:
    # Arrange
    # Act
    _, verdict = renew(None, holder=SRC, token="t", fence=0, now=T0, ttl_s=TTL)
    # Assert
    assert verdict.code == CODE_NOT_HELD


# ---------------------------------------------------------------------------
# handoff — the single atomic point of a relocate
# ---------------------------------------------------------------------------


def test_handoff_moves_the_lease_to_the_target(held: Lease) -> None:
    # Arrange
    # Act
    after, _ = handoff(
        held,
        from_holder=SRC,
        token="tok-src",
        fence=0,
        to_holder=DST,
        to_token="tok-dst",
        now=T0 + 5,
        ttl_s=TTL,
    )
    # Assert
    assert after is not None and after.holder == DST


def test_handoff_advances_the_fence(held: Lease) -> None:
    # Arrange — this is what locks the source out even while its process lives.
    # Act
    after, _ = handoff(
        held,
        from_holder=SRC,
        token="tok-src",
        fence=0,
        to_holder=DST,
        to_token="tok-dst",
        now=T0 + 5,
        ttl_s=TTL,
    )
    # Assert
    assert after is not None and after.fence == held.fence + 1


def test_the_source_cannot_write_after_the_handoff(held: Lease) -> None:
    # Arrange — the property the whole phase order exists for: after the single
    # atomic point there is exactly one writer, even mid-crash.
    after, _ = handoff(
        held,
        from_holder=SRC,
        token="tok-src",
        fence=0,
        to_holder=DST,
        to_token="tok-dst",
        now=T0 + 5,
        ttl_s=TTL,
    )
    # Act
    verdict = check_write(after, holder=SRC, token="tok-src", fence=0, now=T0 + 6)
    # Assert
    assert verdict.allowed is False


def test_the_target_can_write_after_the_handoff(held: Lease) -> None:
    # Arrange
    after, _ = handoff(
        held,
        from_holder=SRC,
        token="tok-src",
        fence=0,
        to_holder=DST,
        to_token="tok-dst",
        now=T0 + 5,
        ttl_s=TTL,
    )
    # Act
    verdict = check_write(after, holder=DST, token="tok-dst", fence=1, now=T0 + 6)
    # Assert
    assert verdict.allowed is True


def test_handoff_refuses_a_caller_that_does_not_hold_the_lease(held: Lease) -> None:
    # Arrange
    # Act
    _, verdict = handoff(
        held,
        from_holder=DST,
        token="tok-src",
        fence=0,
        to_holder="third",
        to_token="t",
        now=T0 + 5,
        ttl_s=TTL,
    )
    # Assert
    assert verdict.code == CODE_HELD_BY_OTHER


def test_handoff_refuses_an_expired_lease(held: Lease) -> None:
    # Arrange — the coordinator no longer holds what it is trying to delegate.
    # Act
    _, verdict = handoff(
        held,
        from_holder=SRC,
        token="tok-src",
        fence=0,
        to_holder=DST,
        to_token="tok-dst",
        now=T0 + TTL + 1,
        ttl_s=TTL,
    )
    # Assert
    assert verdict.code == CODE_EXPIRED


def test_handoff_without_a_lease_reports_not_held() -> None:
    # Arrange
    # Act
    _, verdict = handoff(
        None,
        from_holder=SRC,
        token="t",
        fence=0,
        to_holder=DST,
        to_token="t2",
        now=T0,
        ttl_s=TTL,
    )
    # Assert
    assert verdict.code == CODE_NOT_HELD


# ---------------------------------------------------------------------------
# check_write — three-valued on purpose
# ---------------------------------------------------------------------------


def test_no_lease_record_answers_unknown_not_refusal() -> None:
    # Arrange — folding this into False stops a healthy agent; folding it into
    # True reintroduces the split-brain. It is neither.
    # Act
    verdict = check_write(None, holder=SRC, token="t", fence=0, now=T0)
    # Assert
    assert verdict.allowed is None


def test_the_holder_may_write_while_the_lease_is_live(held: Lease) -> None:
    # Arrange
    # Act
    verdict = check_write(held, holder=SRC, token="tok-src", fence=0, now=T0 + 1)
    # Assert
    assert verdict.allowed is True


def test_a_stale_record_from_the_same_holder_is_refused_by_the_fence(
    held: Lease,
) -> None:
    # Arrange — the case a TTL cannot catch and the holder check does not see.
    # The source paused, its lease expired, and it re-claimed on waking. Its
    # OLD in-memory record has the right NAME and the right TOKEN; only the
    # fence betrays that the world moved on. A holder-name check passes here.
    reclaimed, _ = claim(
        held, agent=AGENT, holder=SRC, token="tok-src", now=T0 + TTL + 1, ttl_s=TTL
    )
    # Act
    verdict = check_write(
        reclaimed, holder=SRC, token="tok-src", fence=held.fence, now=T0 + TTL + 2
    )
    # Assert
    assert verdict.code == CODE_STALE_FENCE


def test_a_fence_ahead_of_the_store_is_refused(held: Lease) -> None:
    # Arrange — a record this store never issued. Accepting it would let a
    # caller invent authority by counting upward.
    # Act
    verdict = check_write(held, holder=SRC, token="tok-src", fence=7, now=T0 + 1)
    # Assert
    assert verdict.code == CODE_STALE_FENCE


def test_an_expired_lease_refuses_writes(held: Lease) -> None:
    # Arrange
    # Act
    verdict = check_write(held, holder=SRC, token="tok-src", fence=0, now=T0 + TTL + 1)
    # Assert
    assert verdict.code == CODE_EXPIRED


def test_a_wrong_token_refuses_writes(held: Lease) -> None:
    # Arrange
    # Act
    verdict = check_write(held, holder=SRC, token="guessed", fence=0, now=T0 + 1)
    # Assert
    assert verdict.code == CODE_WRONG_TOKEN


# ---------------------------------------------------------------------------
# the shapes validate themselves, where they are built
# ---------------------------------------------------------------------------


def test_a_lease_refuses_an_empty_token() -> None:
    # Arrange: an empty token would let any caller pass the token check.
    fields = dict(agent=AGENT, holder=SRC, token="", expires_at=T0, fence=0)

    # Act
    def build() -> Lease:
        return Lease(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_lease_refuses_a_negative_fence() -> None:
    # Arrange: the fence only ever increases, so a negative one is nonsense.
    fields = dict(agent=AGENT, holder=SRC, token="t", expires_at=T0, fence=-1)

    # Act
    def build() -> Lease:
        return Lease(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_a_permission_that_is_not_coded_ok() -> None:
    # Arrange: allowed=True with a refusal code is the shape that lets a caller
    # reading only one field draw the opposite conclusion.
    fields = dict(allowed=True, code=CODE_EXPIRED, reason="contradictory")

    # Act
    def build() -> LeaseVerdict:
        return LeaseVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_an_unknown_that_is_not_coded_unknown() -> None:
    # Arrange: an unknown wearing a success code is how unknown becomes yes.
    fields = dict(allowed=None, code=CODE_OK, reason="contradictory")

    # Act
    def build() -> LeaseVerdict:
        return LeaseVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_an_empty_reason() -> None:
    # Arrange: a refusal with no reason is not actionable.
    fields = dict(allowed=False, code=CODE_EXPIRED, reason="")

    # Act
    def build() -> LeaseVerdict:
        return LeaseVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_refusal_is_truthy_so_callers_must_read_the_field(held: Lease) -> None:
    # Arrange — deliberately NO __bool__: `if verdict:` must not silently pass
    # for a refusal. This test documents the trap rather than hiding it.
    verdict = check_write(held, holder=DST, token="tok-dst", fence=0, now=T0 + 1)
    # Act
    truthy = bool(verdict)
    # Assert
    assert truthy is True
