"""Tests for ``_events._verdicts`` — the ONE routing every sac pass shares.

PA-306: no ``unittest.mock``, no monkeypatching. Real
:class:`SubjectVerdict` values, a REAL JSONL log in ``tmp_path`` driven
through the module's own ``path=`` seam, a real ``io.StringIO`` where the
module expects stderr, and every assertion reads the bytes back through the
production :func:`read_events`. The fail-loud leg breaks the write
ORGANICALLY (a read-only parent dir) rather than injecting a raiser.

The behaviours that matter:

* THREE STATES, NEVER TWO — UNKNOWN is recorded as its own event and can
  never be mistaken for, or folded into, RECOVERED. "I could not look" must
  never read as "I looked and it was fine";
* DEGRADED and UNKNOWN are recorded on EVERY pass, because an ongoing problem
  is an ongoing fact and a log that mentions a wedged agent once then goes
  quiet is indistinguishable from a log written by a pass that died;
* HEALTHY is recorded only on the TRANSITION out of a remembered bad state —
  otherwise ~100 agents would enter the log every five minutes forever and
  bury the events that matter;
* a subject whose record could not be written lands in ``failed`` INSTEAD of
  its state bucket, because sac cannot claim to have recorded what it did not.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container._events import (
    SELF_IMPAIRED,
    SELF_RECOVERED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    SUBJECT_UNKNOWN,
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    degraded_state_path,
    emit_self_state,
    emit_subject_verdicts,
    read_events,
    recover_absent_subjects,
    self_state_path,
)

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0

SUBSYSTEM = "host-sync"


def _log(tmp_path: Path) -> Path:
    """The event log this test writes to. Absent until something writes."""
    return tmp_path / "sac-events.jsonl"


def _readonly(tmp_path: Path) -> Path:
    """An event-log path inside a REALLY read-only dir. No mocks."""
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    return denied / "sac-events.jsonl"


def _degraded(subject: str = "spartan") -> SubjectVerdict:
    return SubjectVerdict(
        subject=subject,
        state=SubjectState.DEGRADED,
        verdict="behind",
        detail=f"{subject} is NOT running the centre's code",
        subject_kind="peer",
    )


def _healthy(subject: str = "spartan") -> SubjectVerdict:
    return SubjectVerdict(
        subject=subject,
        state=SubjectState.HEALTHY,
        verdict="current",
        detail=f"{subject} matches the centre",
        subject_kind="peer",
    )


def _unknown(subject: str = "nas") -> SubjectVerdict:
    return SubjectVerdict(
        subject=subject,
        state=SubjectState.UNKNOWN,
        verdict="unreachable",
        detail=f"could not verify {subject} against the centre",
        subject_kind="peer",
    )


def _kinds(log: Path) -> list[str]:
    return [e.event for e in read_events(log)]


# --- a degraded subject is recorded, every pass ------------------------------


def test_a_degraded_subject_is_recorded(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED]


def test_the_degraded_record_names_the_subject(tmp_path: Path) -> None:
    # Arrange — never silent: a reader must see WHICH peer.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded("spartan")], path=log, now=NOW)
    # Assert
    assert read_events(log)[0].subject == "spartan"


def test_the_degraded_record_keeps_the_pass_verdict(tmp_path: Path) -> None:
    # Arrange — the pass's own token, verbatim and unmapped.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert read_events(log)[0].verdict == "behind"


def test_the_degraded_record_carries_the_subject_kind(tmp_path: Path) -> None:
    # Arrange — a peer, a repo and an agent are not the same population.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert read_events(log)[0].subject_kind == "peer"


def test_an_ongoing_problem_is_recorded_every_pass(tmp_path: Path) -> None:
    # Arrange — a log that mentions a wedged subject ONCE and then goes quiet
    # cannot be told apart from a log written by a pass that died.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED, SUBJECT_DEGRADED]


def test_the_degraded_subject_is_reported_to_the_caller(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    outcome = emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert outcome.degraded == ("spartan",)


# --- recovery is a TRANSITION, not a heartbeat per subject -------------------


def test_degraded_then_healthy_records_one_recovery(tmp_path: Path) -> None:
    # Arrange — THE transition test. The subject was recorded degraded, then
    # it recovered: exactly one of each, in that order, and nothing else.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_the_recovery_is_reported_to_the_caller(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Act
    outcome = emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Assert
    assert outcome.recovered == ("spartan",)


def test_a_never_degraded_healthy_subject_records_nothing(tmp_path: Path) -> None:
    # Arrange — THE noise guard. Writing a record for every healthy subject on
    # every pass would put ~100 agents into the log every five minutes forever
    # and bury the events that matter under the ones that do not.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Assert — not "an empty log": no log at all was ever opened.
    assert not log.exists()


def test_a_healthy_subject_recovers_only_once(tmp_path: Path) -> None:
    # Arrange — a fixed problem stops shouting, and stops announcing that it
    # stopped shouting: the memory is discarded on the transition.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_redegrading_after_a_recovery_records_again(tmp_path: Path) -> None:
    # Arrange — degraded, then well again (memory cleared).
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW)
    # Act — it goes bad AGAIN; the rail must re-fire, not stay silent.
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED, SUBJECT_DEGRADED]


# --- THREE states, never two -------------------------------------------------


def test_an_unknown_subject_is_recorded_as_unknown(tmp_path: Path) -> None:
    # Arrange — "I could not look" must never read as "I looked and it was
    # fine". UNKNOWN is deliberately its own event.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_unknown()], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_UNKNOWN]


def test_an_unknown_subject_is_never_recorded_recovered(tmp_path: Path) -> None:
    # Arrange — the durable false all-clear this rail exists to prevent: a
    # record saying sac looked and found nothing wrong, on the strength of the
    # pass having FAILED to look.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_unknown()], path=log, now=NOW)
    # Assert
    assert SUBJECT_RECOVERED not in _kinds(log)


def test_unknown_and_degraded_are_separate_buckets(tmp_path: Path) -> None:
    # Arrange — an unreadable peer is not a peer without drift; it is a peer
    # whose drift sac failed to observe.
    log = _log(tmp_path)
    # Act
    outcome = emit_subject_verdicts(
        SUBSYSTEM, [_degraded("spartan"), _unknown("nas")], path=log, now=NOW
    )
    # Assert
    assert (outcome.degraded, outcome.unknown) == (("spartan",), ("nas",))


def test_a_remembered_unknown_subject_can_recover(tmp_path: Path) -> None:
    # Arrange — an unobserved subject is remembered like a degraded one, so
    # becoming readable-and-well is a real transition worth recording.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_unknown("nas")], path=log, now=NOW)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_healthy("nas")], path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SUBJECT_UNKNOWN, SUBJECT_RECOVERED]


# --- the memory file sits BESIDE the log it annotates ------------------------


def test_the_degraded_memory_sits_beside_the_log(tmp_path: Path) -> None:
    # Arrange — redirecting the log in a test (or on an operator's host) must
    # carry its state with it automatically, or a suite silently shares one
    # memory file with the live fleet.
    log = _log(tmp_path)
    # Act
    state_file = degraded_state_path(SUBSYSTEM, path=log)
    # Assert
    assert state_file == tmp_path / f"sac-events-{SUBSYSTEM}-degraded.json"


def test_the_degraded_memory_is_persisted(tmp_path: Path) -> None:
    # Arrange — the transition rule needs the memory to survive the process:
    # the passes run as separate processes on a timer.
    log = _log(tmp_path)
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_degraded()], path=log, now=NOW)
    # Assert
    assert "spartan" in degraded_state_path(SUBSYSTEM, path=log).read_text()


def test_a_corrupt_memory_file_is_reported_loudly(tmp_path: Path) -> None:
    # Arrange — a memory sac cannot read means sac has forgotten what it
    # already reported and will re-report it. That must not be silent.
    log = _log(tmp_path)
    degraded_state_path(SUBSYSTEM, path=log).write_text("{not-json")
    stream = io.StringIO()
    # Act
    emit_subject_verdicts(SUBSYSTEM, [_healthy()], path=log, now=NOW, err_stream=stream)
    # Assert
    assert "could not read" in stream.getvalue()


def test_a_corrupt_memory_file_does_not_raise(tmp_path: Path) -> None:
    # Arrange — the memory is an OPTIMISATION OF THE RECORD, never an input to
    # a decision. Losing it costs one duplicated record, never the pass.
    log = _log(tmp_path)
    degraded_state_path(SUBSYSTEM, path=log).write_text("{not-json")
    # Act
    outcome = emit_subject_verdicts(
        SUBSYSTEM, [_degraded()], path=log, now=NOW, err_stream=io.StringIO()
    )
    # Assert
    assert outcome.degraded == ("spartan",)


# --- fail-open: one unwritable file never suppresses the rest ----------------


def test_an_unwritable_log_buckets_the_subject_as_failed(tmp_path: Path) -> None:
    # Arrange — sac cannot claim to have recorded something it did not, so a
    # failed subject lands in ``failed`` INSTEAD of its state bucket.
    log = _readonly(tmp_path)
    try:
        # Act
        outcome = emit_subject_verdicts(
            SUBSYSTEM, [_degraded()], path=log, now=NOW, err_stream=io.StringIO()
        )
        # Assert
        assert outcome.failed == ("spartan",)
    finally:
        log.parent.chmod(0o755)


def test_an_unwritable_log_leaves_the_state_bucket_empty(tmp_path: Path) -> None:
    # Arrange — the other half: a record that failed must not ALSO be counted
    # as recorded, or the summary line becomes a comfortable lie.
    log = _readonly(tmp_path)
    try:
        # Act
        outcome = emit_subject_verdicts(
            SUBSYSTEM, [_degraded()], path=log, now=NOW, err_stream=io.StringIO()
        )
        # Assert
        assert outcome.degraded == ()
    finally:
        log.parent.chmod(0o755)


def test_one_unwritable_record_does_not_suppress_the_rest(tmp_path: Path) -> None:
    # Arrange — the whole log is unwritable, so BOTH subjects fail. Neither
    # may be silently dropped from the outcome.
    log = _readonly(tmp_path)
    try:
        # Act
        outcome = emit_subject_verdicts(
            SUBSYSTEM,
            [_degraded("spartan"), _degraded("nas")],
            path=log,
            now=NOW,
            err_stream=io.StringIO(),
        )
        # Assert
        assert outcome.failed == ("spartan", "nas")
    finally:
        log.parent.chmod(0o755)


def test_an_unwritable_log_is_reported_loudly(tmp_path: Path) -> None:
    # Arrange — a failure nobody hears is the anti-pattern this rail exists
    # to fix.
    log = _readonly(tmp_path)
    stream = io.StringIO()
    try:
        # Act
        emit_subject_verdicts(
            SUBSYSTEM, [_degraded()], path=log, now=NOW, err_stream=stream
        )
        # Assert
        assert "FAILED to record" in stream.getvalue()
    finally:
        log.parent.chmod(0o755)


# --- absence means recovery, and ONLY for a problem-list pass ----------------


def test_absent_remembered_subject_is_recovered(tmp_path: Path) -> None:
    # Arrange — for a pass that reports ONLY the subjects currently in a bad
    # way, a remembered subject's absence IS the observation that it recovered.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded("spartan")], path=log, now=NOW)
    # Act
    recover_absent_subjects(
        SUBSYSTEM, [], detail="no longer drifted", path=log, now=NOW
    )
    # Assert
    assert _kinds(log) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_the_swept_subject_is_reported_to_the_caller(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded("spartan")], path=log, now=NOW)
    # Act
    outcome = recover_absent_subjects(
        SUBSYSTEM, [], detail="no longer drifted", path=log, now=NOW
    )
    # Assert
    assert outcome.recovered == ("spartan",)


def test_a_still_present_subject_is_left_alone(tmp_path: Path) -> None:
    # Arrange — two remembered subjects; this pass still reports one of them,
    # so only the OTHER one has recovered. Sweeping the present one would be a
    # false all-clear about a subject we can still see is broken.
    log = _log(tmp_path)
    emit_subject_verdicts(
        SUBSYSTEM, [_degraded("spartan"), _degraded("nas")], path=log, now=NOW
    )
    # Act
    outcome = recover_absent_subjects(
        SUBSYSTEM, ["spartan"], detail="no longer drifted", path=log, now=NOW
    )
    # Assert
    assert outcome.recovered == ("nas",)


def test_the_present_subject_stays_remembered(tmp_path: Path) -> None:
    # Arrange — the memory must still hold the subject this pass still sees,
    # or its eventual recovery would go unrecorded.
    log = _log(tmp_path)
    emit_subject_verdicts(
        SUBSYSTEM, [_degraded("spartan"), _degraded("nas")], path=log, now=NOW
    )
    # Act
    recover_absent_subjects(
        SUBSYSTEM, ["spartan"], detail="no longer drifted", path=log, now=NOW
    )
    # Assert
    assert "spartan" in degraded_state_path(SUBSYSTEM, path=log).read_text()


def test_sweeping_an_empty_memory_records_nothing(tmp_path: Path) -> None:
    # Arrange — nothing was ever degraded, so nothing can have recovered. The
    # sweep must not invent a record just because it ran.
    log = _log(tmp_path)
    # Act
    recover_absent_subjects(
        SUBSYSTEM, ["spartan"], detail="no longer drifted", path=log, now=NOW
    )
    # Assert
    assert not log.exists()


def test_the_swept_recovery_carries_its_verdict(tmp_path: Path) -> None:
    # Arrange — the sweep's verdict token says HOW the recovery was concluded,
    # which is different evidence from an observed clean reading.
    log = _log(tmp_path)
    emit_subject_verdicts(SUBSYSTEM, [_degraded("spartan")], path=log, now=NOW)
    # Act
    recover_absent_subjects(
        SUBSYSTEM, [], detail="no longer drifted", verdict="absent", path=log, now=NOW
    )
    # Assert
    assert read_events(log, event=SUBJECT_RECOVERED)[0].verdict == "absent"


# --- sac's OWN state: a transition, never a per-tick assertion ---------------
#
# The stakes here are higher than for a subject. A pass runs every few minutes,
# so recording "I am fine" on every tick would write hundreds of thousands of
# records a year and, worse, would make SELF_RECOVERED mean nothing: a recovery
# record has to mark an actual recovery, or it is not evidence of one.


def test_a_self_impairment_is_recorded(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    emit_self_state(
        SUBSYSTEM, impaired=True, detail="cannot read my own memory", path=log, now=NOW
    )
    # Assert
    assert _kinds(log) == [SELF_IMPAIRED]


def test_a_recovery_with_no_prior_impairment_records_nothing(tmp_path: Path) -> None:
    # Arrange — the every-tick case: nothing was ever wrong.
    log = _log(tmp_path)
    # Act
    emit_self_state(SUBSYSTEM, impaired=False, detail="fine", path=log, now=NOW)
    # Assert — not an empty log: no log was ever opened.
    assert not log.exists()


def test_a_recovery_with_no_prior_impairment_reports_nothing_written(
    tmp_path: Path,
) -> None:
    # Arrange — ``False`` means "nothing changed", never "the write failed".
    log = _log(tmp_path)
    # Act
    written = emit_self_state(SUBSYSTEM, impaired=False, detail="fine", path=log)
    # Assert
    assert written is False


def test_a_standing_impairment_is_recorded_every_pass(tmp_path: Path) -> None:
    # Arrange — an ongoing refusal to act is an ongoing fact; a log that
    # mentions it once and goes quiet reads like a log written by a dead pass.
    log = _log(tmp_path)
    emit_self_state(SUBSYSTEM, impaired=True, detail="still denied", path=log, now=NOW)
    # Act
    emit_self_state(SUBSYSTEM, impaired=True, detail="still denied", path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SELF_IMPAIRED, SELF_IMPAIRED]


def test_impaired_then_healthy_records_one_recovery(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    emit_self_state(SUBSYSTEM, impaired=True, detail="denied", path=log, now=NOW)
    # Act
    emit_self_state(
        SUBSYSTEM, impaired=False, detail="readable again", path=log, now=NOW
    )
    # Assert
    assert _kinds(log) == [SELF_IMPAIRED, SELF_RECOVERED]


def test_a_second_recovery_records_nothing(tmp_path: Path) -> None:
    # Arrange — impaired, then recovered (the transition is on the record).
    log = _log(tmp_path)
    emit_self_state(SUBSYSTEM, impaired=True, detail="denied", path=log, now=NOW)
    emit_self_state(SUBSYSTEM, impaired=False, detail="ok", path=log, now=NOW)
    # Act — the next tick is still healthy and must stay quiet.
    emit_self_state(SUBSYSTEM, impaired=False, detail="ok", path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SELF_IMPAIRED, SELF_RECOVERED]


def test_reimpairment_after_a_recovery_records_again(tmp_path: Path) -> None:
    # Arrange — impaired, then well again (memory cleared).
    log = _log(tmp_path)
    emit_self_state(SUBSYSTEM, impaired=True, detail="denied", path=log, now=NOW)
    emit_self_state(SUBSYSTEM, impaired=False, detail="ok", path=log, now=NOW)
    # Act — it breaks AGAIN; the rail must re-fire, not stay silent.
    emit_self_state(SUBSYSTEM, impaired=True, detail="denied", path=log, now=NOW)
    # Assert
    assert _kinds(log) == [SELF_IMPAIRED, SELF_RECOVERED, SELF_IMPAIRED]


def test_the_self_state_file_is_separate_from_the_subject_set(tmp_path: Path) -> None:
    # Arrange — a pass's OWN health is not one of its subjects; sharing one
    # file would need a reserved key a real subject could one day collide with.
    log = _log(tmp_path)
    # Act
    paths = (
        self_state_path(SUBSYSTEM, path=log),
        degraded_state_path(SUBSYSTEM, path=log),
    )
    # Assert
    assert paths[0] != paths[1]


def test_an_unwritable_self_record_reports_failure(tmp_path: Path) -> None:
    # Arrange — a SIDE rail: it must never crash the pass it observes.
    log = _readonly(tmp_path)
    try:
        # Act
        written = emit_self_state(
            SUBSYSTEM,
            impaired=True,
            detail="denied",
            path=log,
            err_stream=io.StringIO(),
        )
        # Assert
        assert written is False
    finally:
        log.parent.chmod(0o755)


# --- the summary line: the caller is never silent ---------------------------


def test_the_summary_line_counts_each_bucket(tmp_path: Path) -> None:
    # Arrange
    outcome = EmitOutcome(degraded=("a",), unknown=("b",), recovered=("c", "d"))
    # Act
    line = outcome.summary_line()
    # Assert
    assert line == "sac events: 1 degraded, 1 unknown, 2 recovered"


def test_the_summary_line_shouts_about_unrecorded(tmp_path: Path) -> None:
    # Arrange — a record that never reached the log is the one thing the
    # summary must not be able to hide.
    outcome = EmitOutcome(degraded=("a",), failed=("b",))
    # Act
    line = outcome.summary_line()
    # Assert
    assert "1 UNRECORDED" in line
