"""Tests for CI-verdict delivery orchestration (sac #404).

feedback.pdf §3: "a2a-deliver the verdict to the pusher, then up the
recorded lineage pusher → parent → … → lead. Job = delivery." This
module composes the data layer (dedup + owner-resolution + lineage
climb) with the a2a ``post`` transport. Every collaborator is a DI seam
so the test drives the full routing/dedup logic with zero network, gh,
or state.db dependency (STX-NM — seams, not mocks).

Conventions: one assertion per test (STX-TQ007); AAA markers.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._ci_deliver import (
    CONSECUTIVE_FAILURE_CAP,
    deliver_verdict,
)


def _seams(
    *, owner="proj-x", ancestors=("lead",), already=False, streak=0, checks=()
):
    """Build a kwargs dict of DI seams + a capture list for posts.

    ``streak`` seams :func:`failures_since_last_success` and ``checks``
    seams :func:`failing_check_names`. Both MUST be injected: without them
    the failure path binds the production readers and the suite starts
    touching the real ``state.db`` and shelling out to ``gh`` (STX-NM —
    seams, not mocks).
    """
    posts: list[str] = []
    recorded: list[tuple] = []
    seams = dict(
        post=lambda name, text: posts.append(name),
        owner_resolver=lambda repo, **kw: owner,
        ancestors=lambda *, name: list(ancestors),
        already_delivered=lambda **kw: already,
        failure_streak=lambda **kw: streak,
        failing_checks=lambda repo, pr: list(checks),
        record=lambda **kw: recorded.append(
            (kw["repo"], kw["pr"], kw["head_sha"], kw["conclusion"])
        ),
    )
    return seams, posts, recorded


def test_pending_verdict_is_skipped_without_posting():
    # Arrange
    seams, posts, _ = _seams()
    # Act
    result = deliver_verdict("o/r", 1, "sha", "pending", **seams)
    # Assert
    assert result["skipped"] is True and posts == []


def test_already_delivered_verdict_is_skipped():
    # Arrange
    seams, posts, _ = _seams(already=True)
    # Act
    result = deliver_verdict("o/r", 1, "sha", "success", **seams)
    # Assert
    assert posts == []


def test_unresolved_owner_is_skipped():
    # Arrange
    seams, posts, _ = _seams(owner=None)
    # Act
    result = deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert result["reason"] == "no-owner"


def test_delivers_to_pusher_then_up_the_lineage_to_lead():
    # Arrange — owner=proj-x, ancestors climb to lead.
    seams, posts, _ = _seams(owner="proj-x", ancestors=("parent", "lead"))
    # Act
    deliver_verdict("o/r", 1, "sha", "success", **seams)
    # Assert
    assert posts == ["proj-x", "parent", "lead"]


def test_records_delivered_after_posting():
    # Arrange
    seams, _, recorded = _seams(owner="proj-x", ancestors=())
    # Act
    deliver_verdict("o/r", 5, "abc", "failure", **seams)
    # Assert
    assert recorded == [("o/r", 5, "abc", "failure")]


def test_one_post_failure_does_not_abort_remaining_targets():
    # Arrange — first target raises; the climb must still reach lead.
    posts: list[str] = []

    def flaky_post(name, text):
        posts.append(name)
        if name == "proj-x":
            raise RuntimeError("boom")

    seams = dict(
        post=flaky_post,
        owner_resolver=lambda repo, **kw: "proj-x",
        ancestors=lambda *, name: ["lead"],
        already_delivered=lambda **kw: False,
        record=lambda **kw: None,
    )
    # Act
    deliver_verdict("o/r", 1, "sha", "success", **seams)
    # Assert
    assert posts == ["proj-x", "lead"]


def test_delivered_message_names_repo_pr_and_conclusion():
    # Arrange — capture the delivered text, not just the target.
    captured: list[str] = []
    seams = dict(
        post=lambda name, text: captured.append(text),
        owner_resolver=lambda repo, **kw: "proj-x",
        ancestors=lambda *, name: [],
        already_delivered=lambda **kw: False,
        failure_streak=lambda **kw: 0,
        record=lambda **kw: None,
    )
    # Act
    deliver_verdict("ywatanabe1989/scitex-dev", 42, "deadbeef", "failure", **seams)
    # Assert
    assert "scitex-dev" in captured[0] and "42" in captured[0]


# --- consecutive-failure cap -------------------------------------------
# Measured 2026-08-16: a standing sync PR whose head ref IS its source
# branch collected fourteen "Red: fix-and-push" deliveries in a day, one
# per unrelated feature merge. Nobody had pushed at it.
#
# The streaks below are LITERALS (2 / 3 / 4), never `CONSECUTIVE_FAILURE_CAP
# ± 1`. Writing them in terms of the constant makes the suite VACUOUS: both
# sides of the `streak == CAP` comparison move together, so changing the cap
# to any value leaves every test green while asserting nothing. Verified by
# sabotage — with `CAP = 10**9` the constant-relative version passed 14/14.
# `test_cap_value_is_pinned` is the single place a cap change must be
# declared deliberately.


def test_cap_value_is_pinned():
    # Arrange — the literals below encode this number; changing the cap
    # without updating them would silently re-vacuum the suite.
    expected = 3
    # Act
    actual = CONSECUTIVE_FAILURE_CAP
    # Assert
    assert actual == expected


def test_red_below_the_cap_still_delivers_normally():
    # Arrange — two reds on record, one short of the cap.
    seams, posts, _ = _seams(streak=2)
    # Act
    result = deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert result["reason"] == "delivered"


def test_red_at_the_cap_escalates():
    # Arrange — the third red is the single escalation tick.
    seams, posts, _ = _seams(streak=3)
    # Act
    result = deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert result["reason"] == "escalated"


def test_escalation_does_not_tell_the_recipient_to_push():
    # Arrange — "fix-and-push" is the instruction that kept the loop alive.
    captured: list[str] = []
    seams, _, _ = _seams(streak=3)
    seams["post"] = lambda name, text: captured.append(text)
    # Act
    deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert "fix-and-push" not in captured[0]


def test_red_past_the_cap_is_silent():
    # Arrange — one past the escalation tick.
    seams, posts, _ = _seams(streak=4)
    # Act
    deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert posts == []


def test_capped_red_is_recorded_but_not_posted():
    # Arrange — if a silenced red were not recorded the count would stall
    # and the very next tick would un-cap the PR. Asserting the RECORD
    # alone does not discriminate: the normal delivery path records too,
    # so that version stays green with the cap disabled. The pair does
    # discriminate, and stays one assertion (STX-TQ007).
    seams, posts, recorded = _seams(streak=4)
    # Act
    deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert (recorded, posts) == ([("o/r", 1, "sha", "failure")], [])


def test_capped_red_never_resolves_an_owner():
    # Arrange — the gate runs before owner resolution, so a silenced PR
    # costs no gh call. A resolver that raises proves it is never reached.
    def exploding_resolver(repo, **kw):
        raise AssertionError("owner resolution ran for a capped PR")

    seams, _, _ = _seams(streak=4)
    seams["owner_resolver"] = exploding_resolver
    # Act
    result = deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert result["reason"] == "streak-capped"


def test_green_is_never_capped_however_long_the_red_streak():
    # Arrange — the cap must not suppress the recovery signal, which is
    # the one message that lets the streak reset.
    seams, posts, _ = _seams(streak=99)
    # Act
    deliver_verdict("o/r", 1, "sha", "success", **seams)
    # Assert
    assert posts == ["proj-x", "lead"]


def test_escalation_names_the_failing_check():
    # Arrange — "check whether the failing check is required" is
    # unanswerable unless the message says WHICH check.
    captured: list[str] = []
    seams, _, _ = _seams(streak=3, checks=("CodeQL",))
    seams["post"] = lambda name, text: captured.append(text)
    # Act
    deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert "CodeQL" in captured[0]


def test_escalation_still_sends_when_the_check_names_are_unavailable():
    # Arrange — naming is a nicety; an unnamed escalation must still go out
    # rather than the resolution failure swallowing the whole message.
    captured: list[str] = []
    seams, _, _ = _seams(streak=3, checks=())
    seams["post"] = lambda name, text: captured.append(text)
    seams["failing_checks"] = lambda repo, pr: (_ for _ in ()).throw(RuntimeError("gh down"))
    # Act
    deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert captured[0].startswith("CI STUCK")


def test_normal_red_never_costs_a_check_name_lookup():
    # Arrange — the extra gh call belongs on the rare escalation tick only,
    # never on the hot path. A lookup that raises proves it is not reached.
    def exploding_lookup(repo, pr):
        raise AssertionError("failing_check_names ran on a non-escalating red")

    seams, _, _ = _seams(streak=0)
    seams["failing_checks"] = exploding_lookup
    # Act
    result = deliver_verdict("o/r", 1, "sha", "failure", **seams)
    # Assert
    assert result["reason"] == "delivered"
