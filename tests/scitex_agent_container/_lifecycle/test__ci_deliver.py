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

from scitex_agent_container._lifecycle._ci_deliver import deliver_verdict


def _seams(*, owner="proj-x", ancestors=("lead",), already=False):
    """Build a kwargs dict of DI seams + a capture list for posts."""
    posts: list[str] = []
    recorded: list[tuple] = []
    seams = dict(
        post=lambda name, text: posts.append(name),
        owner_resolver=lambda repo, **kw: owner,
        ancestors=lambda *, name, db_path=None: list(ancestors),
        already_delivered=lambda **kw: already,
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
        ancestors=lambda *, name, db_path=None: ["lead"],
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
        ancestors=lambda *, name, db_path=None: [],
        already_delivered=lambda **kw: False,
        record=lambda **kw: None,
    )
    # Act
    deliver_verdict("ywatanabe1989/scitex-dev", 42, "deadbeef", "failure", **seams)
    # Assert
    assert "scitex-dev" in captured[0] and "42" in captured[0]
