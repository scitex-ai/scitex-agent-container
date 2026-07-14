"""Tests for the sync verdict model — the refuse/allow decision.

Pure logic, no I/O, so no seams are needed at all. The load-bearing
assertions here are the REFUSALS: AHEAD must never become a merge, and
``--force`` must not buy a destructive git operation.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from scitex_agent_container._hostsync import GraphState, PeerSyncReport, sync_decision


def _report(state: GraphState, **kw) -> PeerSyncReport:
    base = {
        "peer": "spartan",
        "target": "origin/develop",
        "repo": "/data/gpfs/projects/p1/ywatanabe/scitex-agent-container",
    }
    base.update(kw)
    return PeerSyncReport(state=state, **base)


# ---------------------------------------------------------------------------
# the one green light
# ---------------------------------------------------------------------------


def test_behind_and_clean_is_allowed():
    # Arrange
    report = _report(GraphState.BEHIND, behind=5)
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is True


def test_current_peer_is_not_synced_again():
    # Arrange
    report = _report(GraphState.CURRENT)
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# AHEAD is an alarm, not a merge
# ---------------------------------------------------------------------------


def test_ahead_peer_refuses_to_sync():
    # Arrange — the remote holds code the centre does not.
    report = _report(GraphState.AHEAD, ahead=2, ahead_commits=("abc123 hotfix",))
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


def test_ahead_refusal_prints_the_commits_at_stake():
    # Arrange
    report = _report(
        GraphState.AHEAD, ahead=1, ahead_commits=("deadbee fix the thing",)
    )
    # Act
    decision = sync_decision(report)
    # Assert — nobody discards work they never saw.
    assert "deadbee fix the thing" in decision.reason


def test_ahead_refusal_names_it_a_bug_report():
    # Arrange
    report = _report(GraphState.AHEAD, ahead=1, ahead_commits=("abc123 x",))
    # Act
    decision = sync_decision(report)
    # Assert — the message must teach the invariant, not just fail.
    assert "BUG REPORT" in decision.reason


def test_force_does_not_unlock_an_ahead_peer():
    # Arrange — force must never buy a destructive git operation.
    report = _report(GraphState.AHEAD, ahead=3, ahead_commits=("abc123 x",))
    # Act
    decision = sync_decision(report, force=True)
    # Assert
    assert decision.allowed is False


def test_diverged_peer_refuses_to_sync():
    # Arrange
    report = _report(GraphState.DIVERGED, ahead=2, behind=4)
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# dirty tree — never stash, never discard
# ---------------------------------------------------------------------------


def test_dirty_tree_refuses_to_sync():
    # Arrange
    report = _report(GraphState.BEHIND, behind=2, dirty_files=(" M src/foo.py",))
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


def test_dirty_refusal_lists_the_dirty_files():
    # Arrange
    report = _report(GraphState.BEHIND, behind=1, dirty_files=(" M src/live_edit.py",))
    # Act
    decision = sync_decision(report)
    # Assert
    assert "src/live_edit.py" in decision.reason


def test_force_does_not_unlock_a_dirty_tree():
    # Arrange
    report = _report(GraphState.BEHIND, behind=1, dirty_files=(" M src/foo.py",))
    # Act
    decision = sync_decision(report, force=True)
    # Assert
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# UNKNOWN is not clean
# ---------------------------------------------------------------------------


def test_unreachable_peer_is_never_mutated():
    # Arrange — a failed probe told us nothing; it is not a clean peer.
    report = _report(GraphState.UNREACHABLE, detail="ssh timed out")
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


def test_unreachable_refusal_says_state_is_unknown():
    # Arrange
    report = _report(GraphState.UNREACHABLE, detail="ssh timed out")
    # Act
    decision = sync_decision(report)
    # Assert
    assert "UNKNOWN" in decision.reason


def test_wheel_install_peer_is_never_mutated():
    # Arrange — nothing to reconcile: there is no checkout there.
    report = _report(GraphState.NOT_A_CHECKOUT, module="/venv/lib/sac/__init__.py")
    # Act
    decision = sync_decision(report)
    # Assert
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# drift reporting (drives --check's non-zero exit)
# ---------------------------------------------------------------------------


def test_dirty_but_current_peer_counts_as_drifted():
    # Arrange — the object graph agrees, but someone edited the remote.
    report = _report(GraphState.CURRENT, dirty_files=(" M src/foo.py",))
    # Act
    drifted = report.is_drifted
    # Assert
    assert drifted is True


def test_unreachable_peer_is_not_reported_as_drifted():
    # Arrange — "I could not look" must never render as "I looked".
    report = _report(GraphState.UNREACHABLE, detail="ssh timed out")
    # Act
    drifted = report.is_drifted
    # Assert
    assert drifted is False


def test_unreachable_peer_is_reported_as_undetermined():
    # Arrange
    report = _report(GraphState.UNREACHABLE, detail="ssh timed out")
    # Act
    undetermined = report.is_undetermined
    # Assert
    assert undetermined is True
