"""Tests for ``_maintenance._worktree_gc_alarm`` — over-cap → a recorded event.

PA-306: no ``unittest.mock``, no monkeypatching. Real :class:`RepoGcResult`
values and a REAL temporary JSONL event log (``tmp_path/sac-events.jsonl``)
passed through the module's own ``path=`` seam; every assertion reads the
bytes back through the production :func:`read_events`.

The behaviours that matter:

* over cap → a DEGRADED record naming the repo and the count,
* the record carries the kept-reasons BREAKDOWN (the actionable part: "17
  worktrees" is a number, "9 dirty" is an instruction),
* a second over-cap run records AGAIN (an ongoing problem is an ongoing fact),
* back under cap → the RECOVERY is recorded, once,
* UNREADABLE is its own event — never rendered as clean,
* re-sprawl after a recovery records again,
* a write failure NEVER crashes the GC (recording is a side rail).

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from scitex_agent_container._events import (
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    SUBJECT_UNKNOWN,
    read_events,
)
from scitex_agent_container._maintenance._worktree_gc_alarm import (
    SUBSYSTEM,
    record_gc_results,
)
from scitex_agent_container._maintenance._worktree_gc_model import (
    KEEP_DIRTY,
    KEEP_UNMERGED,
    RepoGcResult,
    WorktreeVerdict,
)

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0


def _kept(path: str, *reasons: str) -> WorktreeVerdict:
    return WorktreeVerdict(path=path, branch="feat/x", keep_reasons=tuple(reasons))


def _over_cap(repo: str = "/proj/sprawly", cap: int = 1) -> RepoGcResult:
    """A real result: two survivors against a cap of one."""
    return RepoGcResult(
        repo=repo,
        applied=True,
        cap=cap,
        verdicts=(
            _kept("/wt/a", KEEP_DIRTY),
            _kept("/wt/b", KEEP_UNMERGED),
        ),
    )


def _under_cap(repo: str = "/proj/sprawly", cap: int = 20) -> RepoGcResult:
    return RepoGcResult(
        repo=repo, applied=True, cap=cap, verdicts=(_kept("/wt/a", KEEP_DIRTY),)
    )


def _unreadable(repo: str = "/proj/broken") -> RepoGcResult:
    return RepoGcResult(repo=repo, applied=True, error="not a git repository")


@pytest.fixture
def events(tmp_path: Path) -> Path:
    """A real (initially absent) sac event-log path — no mocks."""
    return tmp_path / "sac-events.jsonl"


@pytest.fixture
def unwritable(tmp_path: Path):
    """An event-log path the REAL writer genuinely cannot write. No mocks."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "sac-events.jsonl"
    finally:
        readonly.chmod(0o755)


def _kinds(events: Path) -> list[str]:
    return [e.event for e in read_events(events, subsystem=SUBSYSTEM)]


def test_over_cap_records_a_degraded_event(events):
    # Arrange — a repo with more survivors than its cap allows.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_the_over_cap_record_names_the_repo(events):
    # Arrange — never silent: a reader must see WHICH repo.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].subject == "sprawly"


def test_the_over_cap_record_carries_the_full_path(events):
    # Arrange — the subject is the readable BASENAME, so the full path has to
    # ride along as a field or two checkouts become indistinguishable.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["repo"] == "/proj/sprawly"


def test_the_over_cap_record_carries_the_count(events):
    # Arrange — two survivors against a cap of one.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["count_after"] == 2


def test_the_over_cap_record_carries_the_reason_breakdown(events):
    # Arrange — "2 kept" is a number; "1 dirty, 1 unmerged" is an instruction.
    # The breakdown is this record's entire value.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["keep_reasons"] == {
        KEEP_DIRTY: 1,
        KEEP_UNMERGED: 1,
    }


def test_the_over_cap_record_labels_the_subject_a_repo(events):
    # Arrange — a repo is not an agent; the subject_kind keeps the populations
    # apart in one shared log.
    # Act
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].subject_kind == "repo"


def test_a_second_over_cap_run_records_again(events):
    # Arrange — first run records the sprawl.
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Act — the nightly timer runs again and the repo is STILL over.
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_DEGRADED]


def test_back_under_cap_records_the_recovery(events):
    # Arrange — the repo sprawled, so a degraded record exists.
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Act — the operator cleaned it up.
    record_gc_results([_under_cap()], path=events, now=NOW)
    # Assert — a fixed repo stops shouting, and says so once.
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_a_healthy_repo_without_prior_sprawl_records_nothing(events):
    # Arrange — a repo that was never over cap.
    # Act
    record_gc_results([_under_cap()], path=events, now=NOW)
    # Assert — no phantom record is created just to say nothing is wrong.
    assert not events.exists()


def test_an_unreadable_repo_is_recorded(events):
    # Arrange — UNKNOWN must be surfaced, not swallowed.
    # Act
    record_gc_results([_unreadable()], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_UNKNOWN]


def test_an_unreadable_repo_is_never_recorded_clean(events):
    # Arrange — "I could not look" must never read as "I looked, it's fine".
    # Act
    record_gc_results([_unreadable()], path=events, now=NOW)
    # Assert
    assert SUBJECT_RECOVERED not in _kinds(events)


def test_the_unreadable_record_carries_the_error(events):
    # Arrange — the reason it could not be read is what sends the operator
    # somewhere useful.
    # Act
    record_gc_results([_unreadable()], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["error"] == "not a git repository"


def test_the_record_buckets_over_cap_and_unknown_separately(events):
    # Arrange — a sprawling repo AND an unreadable one in one run.
    # Act
    outcome = record_gc_results([_over_cap(), _unreadable()], path=events, now=NOW)
    # Assert — three-state honest: distinct buckets, never merged.
    assert (outcome.degraded, outcome.unknown) == (("sprawly",), ("broken",))


def test_the_recovered_repo_is_reported_to_the_caller(events):
    # Arrange — sprawl first so there is something to recover from.
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Act
    outcome = record_gc_results([_under_cap()], path=events, now=NOW)
    # Assert
    assert outcome.recovered == ("sprawly",)


def test_resprawl_after_a_recovery_records_again(events):
    # Arrange — over cap, then cleaned (recovery recorded).
    record_gc_results([_over_cap()], path=events, now=NOW)
    record_gc_results([_under_cap()], path=events, now=NOW)
    # Act — it sprawls AGAIN; the rail must re-fire, not stay silent.
    record_gc_results([_over_cap()], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED, SUBJECT_DEGRADED]


def test_a_write_failure_does_not_raise(unwritable):
    # Arrange — recording is a SIDE rail: it must never crash the GC pass
    # that feeds it.
    # Act
    outcome = record_gc_results(
        [_over_cap()], path=unwritable, now=NOW, err_stream=io.StringIO()
    )
    # Assert — recorded as failed, not raised.
    assert outcome.failed == ("sprawly",)


def test_a_write_failure_is_printed_loudly(unwritable):
    # Arrange — a failure nobody hears is the anti-pattern this whole rail
    # exists to fix, so the side rail still SHOUTS on stderr.
    stream = io.StringIO()
    # Act
    record_gc_results([_over_cap()], path=unwritable, now=NOW, err_stream=stream)
    # Assert
    assert "FAILED to record" in stream.getvalue()


def test_the_subject_is_the_readable_repo_name(events):
    # Arrange — the transition rule is keyed BY SUBJECT, so the label must be
    # stable AND readable: a full path would work but says nothing at a glance.
    # Act
    record_gc_results(
        [_over_cap("/home/user/proj/scitex-agent-container")], path=events, now=NOW
    )
    # Assert
    assert read_events(events)[0].subject == "scitex-agent-container"
