"""Tests for ``_events._log`` — the record sac keeps of sac's own decisions.

PA-306: no ``unittest.mock``, no monkeypatching. Every test drives a REAL
JSONL file in ``tmp_path`` through the module's own ``path=`` seam, reads the
bytes back through the production :func:`read_events`, and injects a real
:class:`io.StringIO` where the module expects stderr. The fail-loud leg breaks
the write ORGANICALLY (a read-only parent directory) rather than injecting a
raiser — the world says no; nothing is faked.

The behaviours that matter:

* a record round-trips: what was written is what a reader gets back;
* ``subject`` / ``subject_kind`` / ``verdict`` are PRESENT-and-null when they
  do not apply, because an absent field ("nobody thought to record it") and a
  null one ("we looked and could not tell") are different facts;
* an unrecognised event name is still recorded but FLAGGED, so a reader can
  tell a new event type from a typo;
* a write that cannot happen returns ``False``, SHOUTS, and never raises —
  losing a log line is bad, losing the pass that line described is worse;
* :func:`log_pass_completed` carries the ``mode`` and the ``counts``, which is
  the only thing distinguishing a healthy fleet from a timer that died.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container._events import (
    EVENT_LOG_ENV,
    PASS_COMPLETED,
    SELF_IMPAIRED,
    SUBJECT_DEGRADED,
    event_log_path,
    log_event,
    log_pass_completed,
    read_events,
)

#: A fixed clock, so no test can be flaky on time and the stamp is assertable.
NOW = 1_800_000_000.0


def _log(tmp_path: Path) -> Path:
    """The event log this test writes to. Absent until something writes."""
    return tmp_path / "sac-events.jsonl"


def _readonly(tmp_path: Path) -> Path:
    """An event-log path inside a REALLY read-only dir. No mocks."""
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    return denied / "sac-events.jsonl"


# --- a record round-trips ---------------------------------------------------


def test_a_recorded_event_round_trips_back(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        subject="spartan",
        subject_kind="peer",
        verdict="behind",
        detail="spartan is 4 commits behind the centre",
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].event == SUBJECT_DEGRADED


def test_the_record_names_its_subsystem(tmp_path: Path) -> None:
    # Arrange — subsystem is the axis a reader filters on FIRST when asking
    # "did my timer run, and what did it decide".
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED, subsystem="host-sync", detail="d", path=log, now=NOW
    )
    # Assert
    assert read_events(log)[0].subsystem == "host-sync"


def test_the_record_names_the_subject(tmp_path: Path) -> None:
    # Arrange — never silent: the operator must see WHICH peer.
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        subject="spartan",
        detail="d",
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].subject == "spartan"


def test_the_pass_verdict_is_kept_verbatim(tmp_path: Path) -> None:
    # Arrange — a verdict translated on the way in can no longer be compared
    # against the code that produced it, so it rides through unmapped.
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        verdict="behind",
        detail="d",
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].verdict == "behind"


def test_the_detail_line_survives_the_round_trip(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        detail="spartan is NOT running the centre's code",
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].detail == "spartan is NOT running the centre's code"


def test_extra_fields_ride_along_as_fields(tmp_path: Path) -> None:
    # Arrange — fields are queryable; a sentence is not.
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        detail="d",
        extra={"target": "origin/develop"},
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].raw["target"] == "origin/develop"


def test_the_injected_clock_stamps_the_record(tmp_path: Path) -> None:
    # Arrange — a fixed clock makes the stamp assertable without sleeping.
    log = _log(tmp_path)
    # Act
    log_event(
        event=SUBJECT_DEGRADED, subsystem="host-sync", detail="d", path=log, now=0.0
    )
    # Assert
    assert read_events(log)[0].timestamp_utc == "1970-01-01T00:00:00+00:00"


def test_two_records_append_rather_than_replace(tmp_path: Path) -> None:
    # Arrange — append-only is the whole contract: a log that overwrote its
    # last line would keep only the least interesting fact it ever held.
    log = _log(tmp_path)
    log_event(event=SELF_IMPAIRED, subsystem="a", detail="1", path=log, now=NOW)
    # Act
    log_event(event=SELF_IMPAIRED, subsystem="a", detail="2", path=log, now=NOW)
    # Assert
    assert [e.detail for e in read_events(log)] == ["1", "2"]


# --- tri-state: present-and-null is a DIFFERENT fact from absent ------------


def test_an_inapplicable_subject_is_present_and_null(tmp_path: Path) -> None:
    # Arrange — a fleet-wide fact belongs to no single subject. The key must
    # still be there: an absent field says nobody thought to record it, a null
    # one says we looked and it does not apply.
    log = _log(tmp_path)
    # Act
    log_event(event=SELF_IMPAIRED, subsystem="fleet-reconcile", detail="d", path=log)
    # Assert — ``"MISSING"`` can only survive if the key is absent.
    assert read_events(log)[0].raw.get("subject", "MISSING") is None


def test_an_inapplicable_subject_kind_is_present_and_null(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    log_event(event=SELF_IMPAIRED, subsystem="fleet-reconcile", detail="d", path=log)
    # Assert
    assert read_events(log)[0].raw.get("subject_kind", "MISSING") is None


def test_an_inapplicable_verdict_is_present_and_null(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    log_event(event=SELF_IMPAIRED, subsystem="fleet-reconcile", detail="d", path=log)
    # Assert
    assert read_events(log)[0].raw.get("verdict", "MISSING") is None


# --- the event vocabulary is closed, but forward-compatible ----------------


def test_an_unrecognised_event_is_flagged_unknown(tmp_path: Path) -> None:
    # Arrange — a record we do not recognise is evidence too, so it is still
    # written; the flag is what lets a reader tell a NEW event from a TYPO.
    log = _log(tmp_path)
    # Act
    log_event(event="subject-degrded", subsystem="host-sync", detail="d", path=log)
    # Assert
    assert read_events(log)[0].raw["event_known"] is False


def test_an_unrecognised_event_is_still_recorded(tmp_path: Path) -> None:
    # Arrange — flagging it must not mean dropping it.
    log = _log(tmp_path)
    # Act
    log_event(event="subject-degrded", subsystem="host-sync", detail="d", path=log)
    # Assert
    assert read_events(log)[0].event == "subject-degrded"


def test_a_known_event_carries_no_unknown_flag(tmp_path: Path) -> None:
    # Arrange — the counterpart: if every record were flagged the flag would
    # measure nothing.
    log = _log(tmp_path)
    # Act
    log_event(event=SUBJECT_DEGRADED, subsystem="host-sync", detail="d", path=log)
    # Assert
    assert "event_known" not in read_events(log)[0].raw


# --- fail-open, but NEVER silent -------------------------------------------


def test_an_unwritable_path_reports_failure(tmp_path: Path) -> None:
    # Arrange — a read-only parent dir: the world says no, nothing injected.
    log = _readonly(tmp_path)
    try:
        # Act
        written = log_event(
            event=SUBJECT_DEGRADED,
            subsystem="host-sync",
            subject="spartan",
            detail="d",
            path=log,
            err_stream=io.StringIO(),
        )
        # Assert
        assert written is False
    finally:
        log.parent.chmod(0o755)


def test_an_unwritable_path_prints_loudly(tmp_path: Path) -> None:
    # Arrange — a logging rail that can fail QUIETLY is worse than no rail,
    # because it is believed. The rail reports its own failure.
    log = _readonly(tmp_path)
    stream = io.StringIO()
    try:
        # Act
        log_event(
            event=SUBJECT_DEGRADED,
            subsystem="host-sync",
            subject="spartan",
            detail="d",
            path=log,
            err_stream=stream,
        )
        # Assert
        assert "[sac-events] FAILED to record" in stream.getvalue()
    finally:
        log.parent.chmod(0o755)


def test_the_loud_line_names_the_lost_subject(tmp_path: Path) -> None:
    # Arrange — "a write failed" is a number; "spartan's drift is now
    # unrecorded" is an instruction.
    log = _readonly(tmp_path)
    stream = io.StringIO()
    try:
        # Act
        log_event(
            event=SUBJECT_DEGRADED,
            subsystem="host-sync",
            subject="spartan",
            detail="d",
            path=log,
            err_stream=stream,
        )
        # Assert
        assert "host-sync/spartan" in stream.getvalue()
    finally:
        log.parent.chmod(0o755)


def test_an_unwritable_path_never_raises(tmp_path: Path) -> None:
    # Arrange — FAIL-OPEN is the contract: the thing observed always outranks
    # the observing of it. Reaching the assertion at all is the proof.
    log = _readonly(tmp_path)
    try:
        # Act
        written = log_event(
            event=SUBJECT_DEGRADED,
            subsystem="host-sync",
            detail="d",
            path=log,
            err_stream=io.StringIO(),
        )
        # Assert
        assert isinstance(written, bool)
    finally:
        log.parent.chmod(0o755)


def test_a_successful_write_reports_success(tmp_path: Path) -> None:
    # Arrange — the counterpart to the failure legs above.
    log = _log(tmp_path)
    # Act
    written = log_event(
        event=SUBJECT_DEGRADED, subsystem="host-sync", detail="d", path=log
    )
    # Assert
    assert written is True


def test_a_successful_write_prints_nothing(tmp_path: Path) -> None:
    # Arrange — a rail that shouts on every healthy tick trains its reader to
    # ignore it, which is how the one line that mattered got scrolled past.
    log = _log(tmp_path)
    stream = io.StringIO()
    # Act
    log_event(
        event=SUBJECT_DEGRADED,
        subsystem="host-sync",
        detail="d",
        path=log,
        err_stream=stream,
    )
    # Assert
    assert stream.getvalue() == ""


def test_a_missing_parent_directory_is_created(tmp_path: Path) -> None:
    # Arrange — a first-ever write on a fresh host must not need a mkdir by
    # hand; a rail that needs installing is a rail that is not installed.
    log = tmp_path / "never" / "existed" / "sac-events.jsonl"
    # Act
    log_event(event=SUBJECT_DEGRADED, subsystem="host-sync", detail="d", path=log)
    # Assert
    assert read_events(log)[0].subsystem == "host-sync"


# --- the pass record: proof the timer ticked at all ------------------------


def test_pass_completed_uses_the_pass_event(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    # Act
    log_pass_completed(subsystem="fleet-reconcile", mode="apply", path=log, now=NOW)
    # Assert
    assert read_events(log)[0].event == PASS_COMPLETED


def test_pass_completed_records_the_mode(tmp_path: Path) -> None:
    # Arrange — load-bearing, not cosmetic: a hand-run dry run writes this
    # record too, so a reader who ignores ``mode`` can believe a scheduled
    # timer is alive on the strength of somebody running the command by hand.
    log = _log(tmp_path)
    # Act
    log_pass_completed(subsystem="fleet-reconcile", mode="dry-run", path=log, now=NOW)
    # Assert
    assert read_events(log)[0].raw["mode"] == "dry-run"


def test_pass_completed_records_the_counts(tmp_path: Path) -> None:
    # Arrange — the counts carry EVERY verdict the pass reached, including the
    # ones that get no per-subject record of their own.
    log = _log(tmp_path)
    # Act
    log_pass_completed(
        subsystem="fleet-reconcile",
        mode="apply",
        counts={"OK": 90, "RESTARTED": 3},
        path=log,
        now=NOW,
    )
    # Assert
    assert read_events(log)[0].raw["counts"] == {"OK": 90, "RESTARTED": 3}


def test_a_clean_pass_still_records_its_counts(tmp_path: Path) -> None:
    # Arrange — "0 restarted, all healthy" is the MOST important record there
    # is: a rail that only writes during trouble cannot tell HEALTHY from DEAD.
    log = _log(tmp_path)
    # Act
    log_pass_completed(subsystem="fleet-reconcile", mode="apply", path=log, now=NOW)
    # Assert
    assert read_events(log)[0].raw["counts"] == {}


def test_pass_completed_failure_does_not_raise(tmp_path: Path) -> None:
    # Arrange — the beacon is a SIDE rail; it must never take the pass down.
    log = _readonly(tmp_path)
    try:
        # Act
        written = log_pass_completed(
            subsystem="fleet-reconcile",
            mode="apply",
            path=log,
            err_stream=io.StringIO(),
        )
        # Assert
        assert written is False
    finally:
        log.parent.chmod(0o755)


# --- reading -----------------------------------------------------------------


def test_read_events_filters_by_subsystem(tmp_path: Path) -> None:
    # Arrange — one log holds every pass; the subsystem is how a reader asks
    # about ONE timer.
    log = _log(tmp_path)
    log_event(event=SUBJECT_DEGRADED, subsystem="host-sync", detail="a", path=log)
    log_event(event=SUBJECT_DEGRADED, subsystem="worktree-gc", detail="b", path=log)
    # Act
    found = read_events(log, subsystem="worktree-gc")
    # Assert
    assert [e.detail for e in found] == ["b"]


def test_read_events_filters_by_event(tmp_path: Path) -> None:
    # Arrange
    log = _log(tmp_path)
    log_event(event=SUBJECT_DEGRADED, subsystem="host-sync", detail="a", path=log)
    log_pass_completed(subsystem="host-sync", mode="apply", path=log)
    # Act
    found = read_events(log, event=PASS_COMPLETED)
    # Assert
    assert [e.event for e in found] == [PASS_COMPLETED]


def test_a_missing_log_reads_as_empty(tmp_path: Path) -> None:
    # Arrange — a missing log is an empty READING, never an error. It is also
    # not evidence that no pass ran, and this function cannot tell those apart.
    # Act
    found = read_events(tmp_path / "never-written.jsonl")
    # Assert
    assert found == []


def test_a_corrupt_line_does_not_hide_the_good_ones(tmp_path: Path) -> None:
    # Arrange — a half-written line (a pass killed mid-write) must not blind a
    # reader to the thousands of records around it during an incident.
    log = _log(tmp_path)
    log_event(event=SUBJECT_DEGRADED, subsystem="host-sync", detail="good", path=log)
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "subject-deg\n')
    # Act
    found = read_events(log)
    # Assert
    assert [e.detail for e in found] == ["good"]


# --- where the log lives ----------------------------------------------------


def test_the_env_var_redirects_the_log_path(tmp_path: Path, env_save_restore) -> None:
    # Arrange — resolved PER CALL, never cached at import: a constant computed
    # at import cannot be redirected by an env var a test sets afterwards,
    # which is how a suite ends up writing into the REAL fleet runtime dir.
    redirected = tmp_path / "elsewhere" / "sac-events.jsonl"
    env_save_restore.set(EVENT_LOG_ENV, str(redirected))
    # Act
    resolved = event_log_path()
    # Assert
    assert resolved == redirected
