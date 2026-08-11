"""A recovery record that names only a host sends someone to a machine to guess.

The operator's item #10 is about what happens when a move goes wrong: the
memory, the temp files and the unfinished git work are still on the source, and
somebody has to find them. So the tests here pin the two properties that decide
whether that is possible — the record must name at least one PATH, and "clean"
must never be confused with "not looked at".

Pure data with validation at construction. No filesystem, no git, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_origin import (
    OriginRecord,
    RepoWork,
    recovery_lines,
)

AGENT = "scitex-agent-container"
SRC = "ywata-note-win"
DST = "scitex-compute-04"
T0 = 1_000_000.0


def _record(**overrides: object) -> OriginRecord:
    base = dict(
        agent=AGENT,
        from_host=SRC,
        to_host=DST,
        at=T0,
        workdir="/home/ywatanabe/proj/scitex-agent-container",
        state_dir="/home/ywatanabe/.scitex/agent-container/agents/sac",
        session_uuid="b68520e1-78fb-404f-a84d-b78cf7cf6e31",
        transcript_path="/home/agent/.claude/projects/x/b68520e1.jsonl",
    )
    base.update(overrides)
    return OriginRecord(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RepoWork — clean is not the same as unscanned
# ---------------------------------------------------------------------------


def test_a_repo_with_uncommitted_files_has_work() -> None:
    # Arrange
    repo = RepoWork(path="/repo", uncommitted=3, unpushed=0)
    # Act
    answer = repo.has_work
    # Assert
    assert answer is True


def test_a_repo_with_unpushed_commits_has_work() -> None:
    # Arrange: pushed-nowhere is unreachable from any other machine.
    repo = RepoWork(path="/repo", uncommitted=0, unpushed=2)
    # Act
    answer = repo.has_work
    # Assert
    assert answer is True


def test_a_measured_clean_repo_has_no_work() -> None:
    # Arrange
    repo = RepoWork(path="/repo", uncommitted=0, unpushed=0)
    # Act
    answer = repo.has_work
    # Assert
    assert answer is False


def test_an_unmeasured_repo_answers_unknown_rather_than_clean() -> None:
    # Arrange: 0 says the repo was clean; None says nobody asked, and the
    # difference decides whether a reader has to go and look.
    repo = RepoWork(path="/repo")
    # Act
    answer = repo.has_work
    # Assert
    assert answer is None


def test_a_repo_with_no_path_is_unrepresentable() -> None:
    # Arrange
    build = lambda: RepoWork(path="")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="path")
    # Assert
    with caught:
        build()


def test_a_negative_count_is_refused_where_it_is_built() -> None:
    # Arrange
    build = lambda: RepoWork(path="/repo", uncommitted=-1)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="uncommitted")
    # Assert
    with caught:
        build()


# ---------------------------------------------------------------------------
# OriginRecord — it must be findable
# ---------------------------------------------------------------------------


def test_a_record_naming_no_path_at_all_is_refused() -> None:
    # Arrange: a row saying only "it came from ywata-note-win" is not a
    # recovery aid.
    build = lambda: OriginRecord(  # noqa: E731
        agent=AGENT, from_host=SRC, to_host=DST, at=T0
    )
    # Act
    caught = pytest.raises(ValueError, match="no idea where to look")
    # Assert
    with caught:
        build()


def test_a_record_with_only_a_repo_is_enough_to_be_findable() -> None:
    # Arrange
    record = OriginRecord(
        agent=AGENT,
        from_host=SRC,
        to_host=DST,
        at=T0,
        repos=(RepoWork(path="/repo", uncommitted=0, unpushed=0),),
    )
    # Act
    where = record.repos[0].path
    # Assert
    assert where == "/repo"


def test_a_record_whose_hosts_are_the_same_is_refused() -> None:
    # Arrange: nothing moved, so there is nothing to recover from.
    build = lambda: _record(to_host=SRC)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="nothing moved")
    # Assert
    with caught:
        build()


def test_a_record_with_no_source_host_is_refused() -> None:
    # Arrange
    build = lambda: _record(from_host="")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="from_host")
    # Assert
    with caught:
        build()


def test_dirty_repos_are_listed_apart_from_unmeasured_ones() -> None:
    # Arrange
    record = _record(
        repos=(
            RepoWork(path="/dirty", uncommitted=4, unpushed=0),
            RepoWork(path="/unknown"),
            RepoWork(path="/clean", uncommitted=0, unpushed=0),
        )
    )
    # Act
    dirty = tuple(r.path for r in record.repos_with_work)
    # Assert
    assert dirty == ("/dirty",)


def test_an_unmeasured_repo_is_not_counted_as_holding_work() -> None:
    # Arrange
    record = _record(
        repos=(RepoWork(path="/dirty", uncommitted=4), RepoWork(path="/unknown"))
    )
    # Act
    unmeasured = tuple(r.path for r in record.repos_unmeasured)
    # Assert
    assert unmeasured == ("/unknown",)


# ---------------------------------------------------------------------------
# recovery_lines — instructions, not a field dump
# ---------------------------------------------------------------------------


def test_the_recovery_lines_start_by_naming_the_machine_to_go_back_to() -> None:
    # Arrange
    record = _record()
    # Act
    lines = recovery_lines(record)
    # Assert
    assert f"ssh {SRC}" in lines[1]


def test_the_recovery_lines_name_the_transcript_path_on_the_source() -> None:
    # Arrange
    record = _record()
    # Act
    rendered = "\n".join(recovery_lines(record))
    # Assert
    assert "/home/agent/.claude/projects/x/b68520e1.jsonl" in rendered


def test_an_uncarried_transcript_is_flagged_as_the_only_copy() -> None:
    # Arrange
    record = _record(transcript_carried=False)
    # Act
    rendered = "\n".join(recovery_lines(record))
    # Assert
    assert "only copy" in rendered


def test_an_unverified_transcript_warns_before_anything_is_deleted() -> None:
    # Arrange: unknown is the case a recovery cares about most.
    record = _record(transcript_carried=None)
    # Act
    rendered = "\n".join(recovery_lines(record))
    # Assert
    assert "UNKNOWN whether it arrived" in rendered


def test_unsaved_work_is_rendered_with_its_counts() -> None:
    # Arrange
    record = _record(
        repos=(RepoWork(path="/repo", branch="feat/x", uncommitted=7, unpushed=2),)
    )
    # Act
    rendered = "\n".join(recovery_lines(record))
    # Assert
    assert "7 uncommitted" in rendered


def test_unmeasured_repos_are_rendered_as_not_measured_not_as_clean() -> None:
    # Arrange
    record = _record(repos=(RepoWork(path="/never-scanned"),))
    # Act
    rendered = "\n".join(recovery_lines(record))
    # Assert
    assert "NOT MEASURED" in rendered
