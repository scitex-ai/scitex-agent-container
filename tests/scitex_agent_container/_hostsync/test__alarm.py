"""Tests for ``_hostsync._alarm`` — drift verdict → a record in sac's own log.

PA-306: no ``unittest.mock``, no monkeypatching. Real :class:`SyncResult` /
:class:`PeerSyncReport` objects and a REAL temporary JSONL event log
(``tmp_path/sac-events.jsonl``) passed through the module's own ``path=``
seam; every assertion reads the bytes back through the production
:func:`read_events`.

The behaviours that matter:

* drift → a DEGRADED record NAMING the peer and its concrete drift class,
* a second drift run records AGAIN (an ongoing problem is an ongoing fact —
  a rail that mentions a stale peer once then goes quiet is indistinguishable
  from a rail that died),
* a clean run RECORDS THE RECOVERY of a previously drifted peer, once,
* UNDETERMINED is its own event — never rendered as clean,
* re-drift after a recovery records again,
* recording is a SIDE rail: an unwritable log is reported, never raised.

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
from scitex_agent_container._hostsync import record_reports
from scitex_agent_container._hostsync._alarm import SUBSYSTEM
from scitex_agent_container._hostsync._model import GraphState, PeerSyncReport
from scitex_agent_container._hostsync._sync import Outcome, SyncResult

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0


def _behind(peer: str = "spartan", behind: int = 4) -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.BEHIND,
        head="aaa111",
        target="origin/develop",
        target_sha="bbb222",
        behind=behind,
        repo="/checkout",
        module="/checkout/src/scitex_agent_container/__init__.py",
        symbol="['agent_name']",
    )
    return SyncResult(peer=peer, outcome=Outcome.DRIFTED, before=report)


def _current(peer: str = "spartan") -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.CURRENT,
        head="aaa111",
        target="origin/develop",
        target_sha="aaa111",
        repo="/checkout",
        module="/checkout/src/scitex_agent_container/__init__.py",
        symbol="['agent_name']",
    )
    return SyncResult(peer=peer, outcome=Outcome.CURRENT, before=report)


def _unreachable(peer: str = "nas") -> SyncResult:
    report = PeerSyncReport(
        peer=peer,
        state=GraphState.UNREACHABLE,
        detail="ssh: connect: refused",
    )
    return SyncResult(peer=peer, outcome=Outcome.UNDETERMINED, before=report)


@pytest.fixture
def events(tmp_path: Path) -> Path:
    """A real (initially absent) sac event-log path — no mocks."""
    return tmp_path / "sac-events.jsonl"


@pytest.fixture
def unwritable(tmp_path: Path):
    """An event-log path the REAL writer genuinely cannot write. No mocks.

    The parent dir is read-only, so the append fails the way it would on a
    broken host — the world says no; nothing is injected.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "sac-events.jsonl"
    finally:
        readonly.chmod(0o755)


def _kinds(events: Path) -> list[str]:
    return [e.event for e in read_events(events, subsystem=SUBSYSTEM)]


def test_drift_records_a_degraded_event(events):
    # Arrange — one peer 4 commits behind the centre.
    # Act
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_the_drift_record_names_the_peer(events):
    # Arrange
    # Act
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert — never silent: a reader must see WHICH peer.
    assert read_events(events)[0].subject == "spartan"


def test_the_drift_record_names_the_concrete_drift(events):
    # Arrange — "behind" is the concrete drift class; the record must say so.
    # Act
    record_reports([_behind("spartan", behind=4)], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].verdict == "behind"


def test_the_drift_record_carries_the_target(events):
    # Arrange — fields are queryable; a sentence is not. ``target`` is what a
    # reader joins on when asking which peers were stale at a given hour.
    # Act
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["target"] == "origin/develop"


def test_the_drift_record_labels_the_subject_a_peer(events):
    # Arrange — a peer is not an agent; the subject_kind keeps the populations
    # apart in one shared log.
    # Act
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].subject_kind == "peer"


def test_a_second_drift_run_records_again(events):
    # Arrange — first run records the drift.
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Act — the peer is STILL stale; an ongoing problem is an ongoing fact.
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_DEGRADED]


def test_a_clean_run_records_the_recovery(events):
    # Arrange — the peer drifted, so a degraded record exists.
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Act — the peer is now current with the centre.
    record_reports([_current("spartan")], path=events, now=NOW)
    # Assert — a fixed drift stops shouting, and says so once.
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_a_clean_peer_without_prior_drift_records_nothing(events):
    # Arrange — a peer that is current and was NEVER drifted.
    # Act
    record_reports([_current("spartan")], path=events, now=NOW)
    # Assert — a well fleet does not write a record per peer per tick.
    assert not events.exists()


def test_an_undetermined_peer_is_recorded(events):
    # Arrange — an unreachable peer. UNKNOWN must be surfaced, not swallowed.
    # Act
    record_reports([_unreachable("nas")], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_UNKNOWN]


def test_an_undetermined_peer_is_never_recorded_clean(events):
    # Arrange — "I could not look" must never read as "I looked, it's fine".
    # Act
    record_reports([_unreachable("nas")], path=events, now=NOW)
    # Assert
    assert SUBJECT_RECOVERED not in _kinds(events)


def test_the_record_buckets_drift_and_unknown_separately(events):
    # Arrange — a drifted peer AND an unreachable peer in one run.
    # Act
    outcome = record_reports(
        [_behind("spartan"), _unreachable("nas")], path=events, now=NOW
    )
    # Assert — drift and unknown are distinct buckets (three-state honest).
    assert (outcome.degraded, outcome.unknown) == (("spartan",), ("nas",))


def test_the_recovered_peer_is_reported_to_the_caller(events):
    # Arrange — drift first so there is something to recover from.
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Act
    outcome = record_reports([_current("spartan")], path=events, now=NOW)
    # Assert
    assert outcome.recovered == ("spartan",)


def test_redrift_after_a_recovery_records_again(events):
    # Arrange — drift, then fixed (recovery recorded).
    record_reports([_behind("spartan")], path=events, now=NOW)
    record_reports([_current("spartan")], path=events, now=NOW)
    # Act — the peer drifts AGAIN; the rail must re-fire, not stay silent.
    record_reports([_behind("spartan")], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED, SUBJECT_DEGRADED]


def test_an_unwritable_log_does_not_raise(events, unwritable):
    # Arrange — recording is a SIDE rail: it must never crash the read-only
    # check that feeds it.
    # Act
    outcome = record_reports(
        [_behind("spartan")], path=unwritable, now=NOW, err_stream=io.StringIO()
    )
    # Assert — recorded as failed, not raised.
    assert outcome.failed == ("spartan",)


def test_an_unwritable_log_is_printed_loudly(events, unwritable):
    # Arrange — a failure nobody hears is the anti-pattern this whole rail
    # exists to fix, so the side rail still SHOUTS on stderr.
    stream = io.StringIO()
    # Act
    record_reports([_behind("spartan")], path=unwritable, now=NOW, err_stream=stream)
    # Assert
    assert "FAILED to record" in stream.getvalue()
