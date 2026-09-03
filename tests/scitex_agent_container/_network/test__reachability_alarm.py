"""Tests for ``_network/_reachability_alarm`` — verdicts into sac's event log.

No mocks: real :class:`HostReachability` rows and a REAL temporary JSONL
event log passed through the module's own ``path=`` seam; every assertion
reads the bytes back through the production :func:`read_events` — the same
rail ``fleet-reconcile`` and ``host-sync-check`` record to.

The behaviours that matter:

* unreachable → a DEGRADED record naming the host, under this pass's own
  subsystem axis;
* unknown → its OWN event, never rendered as healthy;
* per host, a record on TRANSITION only: two identical passes write one
  record; a change of state writes one more (review round 2 of PR #1285 —
  the shared rail's every-pass re-record put ten unknown records into the
  log every fifteen minutes on compute-04);
* THIS host's row is skipped by the ``local`` flag the probe carried — NOT
  by comparing its name to ``probed_from``, which can be spelled differently
  on the same machine;
* reachable after a remembered degraded → RECOVERED, once; reachable on a
  clean slate → nothing (a transition, not a heartbeat);
* every pass writes a PASS_COMPLETED record carrying the counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._events import (
    PASS_COMPLETED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    SUBJECT_UNKNOWN,
    read_events,
)
from scitex_agent_container._network._reachability import (
    TRANSPORT_NONE,
    TRANSPORT_SSH,
    HostReachability,
    ReachabilityReport,
)
from scitex_agent_container._network._reachability_alarm import (
    SUBSYSTEM,
    last_state_path,
    record_pass_completed,
    record_report,
)

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0


def _row(
    host: str, reachable, transport: str = TRANSPORT_SSH, *, local: bool = False
) -> HostReachability:
    return HostReachability(
        host=host,
        ssh_alias=None if transport == TRANSPORT_NONE else f"{host}-ssh",
        transport=transport,
        reachable=reachable,
        elapsed_ms=None if reachable is None else 12,
        error=None if reachable is True else "ssh: connect: refused",
        local=local,
    )


def _report(
    *rows: HostReachability, probed_from: str = "probe-box"
) -> ReachabilityReport:
    return ReachabilityReport(
        probed_from=probed_from,
        port=7878,
        started_at_utc="2026-09-02T00:00:00+00:00",
        elapsed_ms=5,
        rows=tuple(rows),
    )


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "sac-events.jsonl"


def _events(log: Path, event: str):
    return [e for e in read_events(path=log) if e.event == event]


def test_unreachable_host_is_recorded_degraded(log):
    # Arrange
    report = _report(_row("peer-a", False))
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert [e.subject for e in _events(log, SUBJECT_DEGRADED)] == ["peer-a"]


def test_degraded_record_speaks_under_this_pass_own_subsystem(log):
    # Arrange — a reader filters the shared log on this axis first.
    report = _report(_row("peer-a", False))
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert _events(log, SUBJECT_DEGRADED)[0].subsystem == SUBSYSTEM


def test_unknown_host_is_recorded_as_unknown_not_healthy(log):
    # Arrange
    report = _report(_row("peer-b", None))
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert [e.subject for e in _events(log, SUBJECT_UNKNOWN)] == ["peer-b"]


# --- transitions, not a heartbeat per host ----------------------------------


def test_two_identical_degraded_passes_write_one_record(log):
    # Arrange — an unchanged fact is in the --record file; the log holds
    # the change.
    report = _report(_row("peer-a", False))
    # Act
    record_report(report, path=log, now=NOW)
    record_report(report, path=log, now=NOW + 900)
    # Assert
    assert len(_events(log, SUBJECT_DEGRADED)) == 1


def test_two_identical_unknown_passes_write_one_record(log):
    # Arrange — THE measured noise: ten unknown peers, re-recorded every tick.
    report = _report(_row("peer-b", None))
    # Act
    record_report(report, path=log, now=NOW)
    record_report(report, path=log, now=NOW + 900)
    # Assert
    assert len(_events(log, SUBJECT_UNKNOWN)) == 1


def test_a_change_from_unknown_to_degraded_writes_one_more_record(log):
    # Arrange — a token arrived and the leg was dispatched and failed: a
    # different fact, so a second record.
    record_report(_report(_row("peer-a", None)), path=log, now=NOW)
    # Act
    record_report(_report(_row("peer-a", False)), path=log, now=NOW + 900)
    # Assert
    assert [e.event for e in read_events(path=log)] == [
        SUBJECT_UNKNOWN,
        SUBJECT_DEGRADED,
    ]


def test_a_change_from_degraded_to_unknown_writes_one_more_record(log):
    # Arrange — the token was removed: "could not probe" is not "still down".
    record_report(_report(_row("peer-a", False)), path=log, now=NOW)
    # Act
    record_report(_report(_row("peer-a", None)), path=log, now=NOW + 900)
    # Assert
    assert [e.event for e in read_events(path=log)] == [
        SUBJECT_DEGRADED,
        SUBJECT_UNKNOWN,
    ]


def test_a_host_unchanged_across_three_passes_is_still_recorded_once(log):
    # Arrange
    report = _report(_row("peer-a", False), _row("peer-b", None))
    # Act
    for tick in range(3):
        record_report(report, path=log, now=NOW + 900 * tick)
    # Assert
    assert len(read_events(path=log)) == 2


def test_a_lost_memory_re_records_the_host_once_and_nothing_worse(log):
    # Arrange — the memory is an optimisation of the record: losing it
    # costs one duplicate, never a missed change.
    report = _report(_row("peer-a", False))
    record_report(report, path=log, now=NOW)
    last_state_path(path=log).unlink()
    # Act
    record_report(report, path=log, now=NOW + 900)
    record_report(report, path=log, now=NOW + 1800)
    # Assert
    assert len(_events(log, SUBJECT_DEGRADED)) == 2


# --- this host: skipped by the carried flag, never by name ------------------


def test_this_host_own_row_writes_no_record(log):
    # Arrange — unknown by construction, not by failure to look.
    report = _report(_row("probe-box", None, TRANSPORT_NONE, local=True))
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert read_events(path=log) == []


def test_this_host_is_skipped_by_its_local_flag_not_by_its_name(log):
    # Arrange — the measured divergence: canonical_host() says the factory
    # hostname, the registry (which decided `local`) says the fleet name.
    # A name comparison would record this row as unknown every pass.
    report = _report(
        _row("scitex-nas-03", None, TRANSPORT_NONE, local=True),
        probed_from="DXP480TPLUS-994",
    )
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert read_events(path=log) == []


def test_reachable_on_a_clean_slate_writes_nothing(log):
    # Arrange — a transition rail, not a per-host heartbeat.
    report = _report(_row("peer-a", True))
    # Act
    record_report(report, path=log, now=NOW)
    # Assert
    assert read_events(path=log) == []


def test_reachable_after_degraded_records_the_recovery(log):
    # Arrange
    record_report(_report(_row("peer-a", False)), path=log, now=NOW)
    # Act
    record_report(_report(_row("peer-a", True)), path=log, now=NOW + 900)
    # Assert
    assert [e.subject for e in _events(log, SUBJECT_RECOVERED)] == ["peer-a"]


def test_recovery_is_recorded_once_not_on_every_later_pass(log):
    # Arrange
    record_report(_report(_row("peer-a", False)), path=log, now=NOW)
    record_report(_report(_row("peer-a", True)), path=log, now=NOW + 900)
    # Act
    record_report(_report(_row("peer-a", True)), path=log, now=NOW + 1800)
    # Assert
    assert len(_events(log, SUBJECT_RECOVERED)) == 1


def test_outcome_names_what_reached_the_log(log):
    # Arrange
    report = _report(_row("peer-a", False), _row("peer-b", None), _row("me", True))
    # Act
    outcome = record_report(report, path=log, now=NOW)
    # Assert
    assert (outcome.degraded, outcome.unknown) == (("peer-a",), ("peer-b",))


def test_pass_completed_record_carries_the_counts(log):
    # Arrange
    report = _report(_row("peer-a", False), _row("peer-b", None), _row("peer-c", True))
    # Act
    record_pass_completed(report, mode="all", path=log, now=NOW)
    counts = _events(log, PASS_COMPLETED)[0].raw["counts"]
    # Assert
    assert counts == {"hosts": 3, "reachable": 1, "unreachable": 1, "unknown": 1}


def test_pass_completed_record_carries_the_mode(log):
    # Arrange — a by-hand subset run must not read as the timer being alive.
    report = _report(_row("peer-a", True))
    # Act
    record_pass_completed(report, mode="subset", path=log, now=NOW)
    # Assert
    assert _events(log, PASS_COMPLETED)[0].verdict == "subset"
