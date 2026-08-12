"""Both of these refused correctly on the canary, and both refused after the agent was down.

Two checks, one property: the phase that needs the answer runs late, so the
question is asked here instead.

    lease_holdable        HANDOVER is the last phase before DONE. Measured
                          2026-08-11 on the canary's return leg: exit 5 there,
                          after source_stop, after the transport was verified,
                          after the standby booted and after the handshake
                          passed. Nothing was running on either host.
    target_start_accepts  TARGET_STANDBY calls the target's own ``sac agents
                          start``, which refused with ``sac-drift: spec source
                          is 1 commit(s) BEHIND``. Preflight had eleven checks
                          about that host and none of them had asked its start
                          command anything.

The tests are written against those exact states. A check nobody has seen fail on
the input that produced it is a check nobody knows would have caught it.

Pure predicates over observed facts. No I/O, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_checks_late import (
    CHECK_LEASE,
    CHECK_TARGET_START,
    check_lease_holdable,
    check_target_start,
)
from scitex_agent_container._lifecycle._relocate_lease import Lease
from scitex_agent_container._lifecycle._relocate_preflight_facts import (
    LeaseFacts,
    SpecSourceDrift,
    TargetFacts,
)

AGENT = "canary-resume-test"
A = "scitex-compute-04"
B = "ywata-note-win"
NOW = 1_786_500_000.0
TTL = 86_400.0


def _row(holder: str, *, expires_at: float = NOW + TTL) -> Lease:
    return Lease(
        agent=AGENT, holder=holder, token="tok", expires_at=expires_at, fence=1
    )


@pytest.fixture
def canary_return_leg() -> LeaseFacts:
    """What the coordinator on ywata-note-win read out of its OWN db, unobserved.

    The row was written 2026-08-11 13:20 UTC by an earlier move. It says
    ``holder=scitex-compute-04`` and it says nothing whatever about today.
    """
    return LeaseFacts(read=True, lease=_row(A), store="/…/runtime/state.db", now=NOW)


# ---------------------------------------------------------------------------
# lease_holdable
# ---------------------------------------------------------------------------


def test_a_store_nobody_opened_is_unknown() -> None:
    # Arrange: the default — a caller that has not looked has established nothing.
    facts = LeaseFacts()
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is None


def test_an_unread_store_names_the_table_to_read() -> None:
    # Arrange
    facts = LeaseFacts()
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert "relocation_leases" in check.hint


def test_a_read_store_with_no_row_passes() -> None:
    # Arrange: no row is a real answer, and it bootstraps.
    facts = LeaseFacts(read=True, lease=None, now=NOW)
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is True


def test_a_row_read_with_no_clock_cannot_answer_expiry() -> None:
    # Arrange
    facts = LeaseFacts(read=True, lease=_row(B), now=None)
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is None


def test_an_unresolved_source_host_is_unknown() -> None:
    # Arrange: "may THIS host hand over" needs the host named.
    facts = LeaseFacts(read=True, lease=_row(B), now=NOW)
    # Act
    check = check_lease_holdable(facts, "", AGENT)
    # Assert
    assert check.ok is None


def test_the_canary_return_leg_is_unknown_rather_than_a_refusal(
    canary_return_leg: LeaseFacts,
) -> None:
    # Arrange: the exact input that produced exit 5 at handover.
    facts = canary_return_leg
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is None


def test_the_unknown_explains_that_the_row_is_written_on_the_host_being_left(
    canary_return_leg: LeaseFacts,
) -> None:
    # Arrange: without this sentence the reader cannot tell a stale row from a
    # live second writer, which is the whole confusion.
    facts = canary_return_leg
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert "COORDINATOR" in check.hint


def test_an_observed_absent_holder_passes(canary_return_leg: LeaseFacts) -> None:
    # Arrange: somebody looked at scitex-compute-04 and it is not running it.
    facts = LeaseFacts(
        read=True,
        lease=_row(A),
        recorded_holder_running=False,
        recorded_holder_evidence="tmux on scitex-compute-04 has NO session",
        now=NOW,
    )
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is True


def test_an_observed_running_holder_fails() -> None:
    # Arrange: this is the split-brain the lease exists to catch.
    facts = LeaseFacts(
        read=True,
        lease=_row(A),
        recorded_holder_running=True,
        recorded_holder_evidence="tmux on scitex-compute-04 has session tui-…",
        now=NOW,
    )
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.ok is False


def test_the_split_brain_refusal_forbids_forcing_the_handover() -> None:
    # Arrange
    facts = LeaseFacts(
        read=True, lease=_row(A), recorded_holder_running=True, now=NOW
    )
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert "Do NOT force" in check.hint


def test_the_split_brain_refusal_quotes_what_was_seen() -> None:
    # Arrange: a refusal on an observation must show the observation.
    facts = LeaseFacts(
        read=True,
        lease=_row(A),
        recorded_holder_running=True,
        recorded_holder_evidence="tmux on scitex-compute-04 has session tui-canary",
        now=NOW,
    )
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert "tui-canary" in check.detail


def test_the_check_reports_which_store_it_read() -> None:
    # Arrange: a lease answer is worth exactly as much as the db it came from,
    # and this fleet has one db per host with no sync between them.
    facts = LeaseFacts(read=True, lease=None, store="/state/x/state.db", now=NOW)
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert "/state/x/state.db" in check.detail


def test_the_lease_check_is_named_for_the_report() -> None:
    # Arrange
    facts = LeaseFacts(read=True, lease=None, now=NOW)
    # Act
    check = check_lease_holdable(facts, B, AGENT)
    # Assert
    assert check.name == CHECK_LEASE


# ---------------------------------------------------------------------------
# target_start_accepts
# ---------------------------------------------------------------------------


@pytest.fixture
def compute_04_dotfiles() -> SpecSourceDrift:
    """The live state on scitex-compute-04, measured 2026-08-12.

    Five commits behind with 2389 modified files — which matters because the
    remedy the guard prints is ``git pull --ff-only``, and that aborts here.
    """
    return SpecSourceDrift(
        state="behind",
        behind=5,
        ahead=0,
        repo="/home/ywatanabe/.dotfiles",
        upstream="origin/main",
        dirty=2389,
    )


def test_an_unasked_target_is_unknown() -> None:
    # Arrange: eleven checks about the target and none asked its start command.
    facts = TargetFacts()
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert check.ok is None


def test_the_unasked_answer_names_the_command_that_would_tell_us() -> None:
    # Arrange
    facts = TargetFacts()
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert "sac doctor" in check.hint


def test_a_current_spec_source_passes() -> None:
    # Arrange
    facts = TargetFacts(
        spec_source_drift=SpecSourceDrift(state="current", upstream="origin/main")
    )
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert check.ok is True


def test_an_AHEAD_spec_source_passes() -> None:
    # Arrange: the guard refuses on staleness only. AHEAD means the spec about to
    # launch is the newest one that exists — it merely has not propagated.
    facts = TargetFacts(
        spec_source_drift=SpecSourceDrift(state="ahead", ahead=3, repo="/r")
    )
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert check.ok is True


def test_a_not_a_repo_spec_source_passes() -> None:
    # Arrange: drift is UNKNOWN to the guard there, and it never refuses on it.
    facts = TargetFacts(spec_source_drift=SpecSourceDrift(state="not-a-repo"))
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert check.ok is True


def test_a_BEHIND_spec_source_fails(compute_04_dotfiles: SpecSourceDrift) -> None:
    # Arrange: what cost the canary its first leg, before source_stop this time.
    facts = TargetFacts(spec_source_drift=compute_04_dotfiles)
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert check.ok is False


def test_a_DIVERGED_spec_source_fails() -> None:
    # Arrange: stale AND unpushed — the guard's other refusing state.
    facts = TargetFacts(
        spec_source_drift=SpecSourceDrift(
            state="diverged", behind=2, ahead=1, repo="/r", upstream="origin/main"
        )
    )
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert check.ok is False


def test_the_refusal_names_the_repo_to_pull_on_the_target(
    compute_04_dotfiles: SpecSourceDrift,
) -> None:
    # Arrange
    facts = TargetFacts(spec_source_drift=compute_04_dotfiles)
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert "git -C /home/ywatanabe/.dotfiles pull --ff-only" in check.hint


def test_the_refusal_warns_that_ff_only_aborts_on_this_dirty_tree(
    compute_04_dotfiles: SpecSourceDrift,
) -> None:
    # Arrange: a hint naming a command that will not run costs the same trip as
    # no hint at all.
    facts = TargetFacts(spec_source_drift=compute_04_dotfiles)
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert "2389 modified file(s) and --ff-only" in check.hint


def test_a_clean_behind_repo_is_not_warned_about_a_dirty_tree() -> None:
    # Arrange: the caveat must not appear where it does not apply.
    facts = TargetFacts(
        spec_source_drift=SpecSourceDrift(
            state="behind", behind=1, repo="/r", upstream="origin/main", dirty=0
        )
    )
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert "--ff-only aborts" not in check.hint


def test_the_refusal_names_the_override_without_recommending_it() -> None:
    # Arrange
    facts = TargetFacts(
        spec_source_drift=SpecSourceDrift(state="behind", behind=1, repo="/r")
    )
    # Act
    check = check_target_start(facts, A, AGENT)
    # Assert
    assert "prefer the pull" in check.hint


def test_the_start_check_is_named_for_the_report() -> None:
    # Arrange
    facts = TargetFacts(spec_source_drift=SpecSourceDrift(state="current"))
    # Act
    check = check_target_start(facts, B, AGENT)
    # Assert
    assert check.name == CHECK_TARGET_START


def test_a_drift_answer_with_no_state_is_refused_at_construction() -> None:
    # Arrange / Act / Assert: a verdict with no verdict is not one the target's
    # start command could have produced.
    # Act
    # Assert
    with pytest.raises(ValueError, match="state must be non-empty"):
        SpecSourceDrift(state="")
