"""A failed `git status` prints nothing. So does a clean tree. Counting lines conflates them.

That is the one mistake this scanner exists to not make: reading an empty stdout
as "0 uncommitted files" turns a git that could not run into a confident
"nothing to strand", and the relocation proceeds away from work it was supposed
to be protecting.

The second is subtler and costs more: a branch with NO UPSTREAM makes
`rev-list @{u}..HEAD` fail, and reporting 0 there would clear exactly the branch
that exists on one machine only.

The runner is a real callable returning real captured git output. Nothing is
mocked, no repo is created, and no subprocess runs.
"""

from __future__ import annotations

from typing import Sequence

from scitex_agent_container._lifecycle._relocate_source_scan import (
    CommandResult,
    scan_repo,
    scan_source,
)

REPO = "/home/ywatanabe/proj/scitex-agent-container"


def _runner(answers: dict[str, CommandResult]):
    """Answer by git subcommand, so a test states only what it cares about."""

    def run(argv: Sequence[str]) -> CommandResult:
        key = argv[3]
        return answers.get(key, CommandResult(stdout="", stderr="", exit_code=1))

    return run


def _ok(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", exit_code=0)


def _broken(stderr: str) -> CommandResult:
    return CommandResult(stdout="", stderr=stderr, exit_code=128)


def _healthy(**overrides: CommandResult):
    answers = {
        "rev-parse": _ok("develop\n"),
        "status": _ok(""),
        "rev-list": _ok("0\n"),
    }
    answers.update(overrides)
    return _runner(answers)


# ---------------------------------------------------------------------------
# counting what is there
# ---------------------------------------------------------------------------


def test_a_clean_repo_reports_zero_uncommitted() -> None:
    # Arrange
    runner = _healthy()
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.uncommitted == 0


def test_modified_files_are_counted_one_per_porcelain_line() -> None:
    # Arrange
    runner = _healthy(status=_ok(" M src/a.py\n?? new.py\n M src/b.py\n"))
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.uncommitted == 3


def test_commits_ahead_of_the_upstream_are_counted() -> None:
    # Arrange
    runner = _healthy(**{"rev-list": _ok("4\n")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.unpushed == 4


def test_the_branch_name_is_recorded_for_the_recovery_record() -> None:
    # Arrange
    runner = _healthy(**{"rev-parse": _ok("feat/relocate-execute-path\n")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.branch == "feat/relocate-execute-path"


def test_a_clean_scanned_repo_holds_no_work() -> None:
    # Arrange
    runner = _healthy()
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.has_work is False


# ---------------------------------------------------------------------------
# a failed command leaves its count UNMEASURED, never zero
# ---------------------------------------------------------------------------


def test_a_failed_status_leaves_the_file_count_unmeasured() -> None:
    # Arrange: an empty stdout from a FAILED status is indistinguishable from a
    # clean tree, and counting it as 0 is the whole failure this guards.
    runner = _healthy(status=_broken("fatal: not a git repository"))
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.uncommitted is None


def test_a_branch_with_no_upstream_leaves_the_unpushed_count_unmeasured() -> None:
    # Arrange: `rev-list @{u}..` fails with no upstream. Reporting 0 would clear
    # exactly the branch that exists on one machine only.
    runner = _healthy(**{"rev-list": _broken("fatal: no upstream configured")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.unpushed is None


def test_a_repo_whose_counts_all_failed_answers_unknown_rather_than_clean() -> None:
    # Arrange
    runner = _healthy(status=_broken("fatal"), **{"rev-list": _broken("fatal")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.has_work is None


def test_a_non_numeric_ahead_count_is_unmeasured_not_zero() -> None:
    # Arrange: garbage on stdout with exit 0 is still not a number.
    runner = _healthy(**{"rev-list": _ok("not a number\n")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.unpushed is None


def test_a_failed_branch_lookup_costs_only_the_branch_name() -> None:
    # Arrange: three independent questions, so one failure does not cost the
    # others — a repo whose branch is unreadable is still worth counting.
    runner = _healthy(**{"rev-parse": _broken("fatal")})
    # Act
    repo = scan_repo(REPO, runner=runner)
    # Assert
    assert repo.uncommitted == 0


# ---------------------------------------------------------------------------
# the whole source
# ---------------------------------------------------------------------------


def test_scanning_several_repos_produces_one_entry_each() -> None:
    # Arrange
    runner = _healthy()
    # Act
    facts = scan_source(["/a", "/b", "/c"], runner=runner)
    # Assert
    assert len(facts.repos or ()) == 3


def test_scanning_no_repos_is_an_observed_empty_rather_than_nothing_observed() -> None:
    # Arrange: () passes the preflight check; None refuses it. The difference is
    # deliberate and the caller must state which it means.
    runner = _healthy()
    # Act
    facts = scan_source([], runner=runner)
    # Assert
    assert facts.repos == ()
