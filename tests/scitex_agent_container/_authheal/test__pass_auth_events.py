"""A real pass leaves an auth-event trail that can DISAGREE with the pass.

This is the suite the whole PR exists for. It drives the production
:func:`auth_heal_pass` over real captured panes, a real temp history file and a
real temp registry — the only injected seam is the restart itself, a
:class:`Recorder` with the production ``(name) -> bool`` signature — and then
reads the real JSONL bytes back off disk.

WHAT IT PROVES, AND WHY THAT WAS NOT PROVABLE BEFORE
    ``auth-heal.log`` recorded 169 ``-> auto-restart`` lines over seven days
    whose ``age=`` field never reset. Every line stated an INTENT in the
    grammar of an EFFECT, and no record existed that could contradict one. The
    decisive test here is
    :func:`test_a_restart_that_does_not_take_effect_is_visible_as_unresolved`:
    a restart is attempted, it does NOT take, and the log must be readable as
    attempt-without-successful-outcome.

MUTATION-PROOF
    Collapse the two emissions in ``_pass._perform`` into one combined
    "restarted" event and these tests go red — the attempt/outcome pair
    disappears and ``unresolved_attempts`` can no longer see the failure. That
    is the intended failure mode: the test is pinned to the SEPARATION, not to
    the fact that something was written.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._authevents import (
    AUTH_FAILURE_OBSERVED,
    RESTART_ATTEMPTED,
    RESTART_OUTCOME,
    read_auth_events,
    unresolved_attempts,
)
from scitex_agent_container._authheal._pass import auth_heal_pass

from ._helpers import NOW, Recorder, stuck


def _events(path: Path, kind: str) -> list:
    return [e for e in read_auth_events(path) if e.event == kind]


def test_a_restart_that_does_not_take_effect_is_visible_as_unresolved(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """THE test: a restart that ran and did not work must be refutable.

    The recorder returns False — the production signature's way of saying the
    restart reported failure — so the pass really does attempt a restart that
    really does not take. The log must show the attempt AND an outcome that
    contradicts it, and the refutation query must surface it.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=False)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    unresolved = unresolved_attempts(read_auth_events(event_log))
    assert [e.agent for e in unresolved] == ["figrecipe"]


def test_the_attempt_and_the_outcome_are_two_separate_records(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """Conflating them is the exact defect this rail exists to prevent.

    One restart, two records. If ``_perform`` ever writes a single combined
    event this assertion is the one that catches it.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=False)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    attempts = _events(event_log, RESTART_ATTEMPTED)
    outcomes = _events(event_log, RESTART_OUTCOME)
    assert (len(attempts), len(outcomes)) == (1, 1)


def test_the_failed_outcome_records_that_it_did_not_succeed(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """The outcome must carry the refutation explicitly, not by omission."""
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=False)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    assert _events(event_log, RESTART_OUTCOME)[0].succeeded is False


def test_a_successful_restart_leaves_nothing_unresolved(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """The refutation query must be able to come back clean.

    Without this, the failing test above would pass under a rail that reports
    EVERY restart as unresolved — which would measure nothing at all.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    assert unresolved_attempts(read_auth_events(event_log)) == []


def test_the_attempt_is_recorded_before_the_outcome(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """Order is load-bearing: intent is written BEFORE the act it describes.

    A restart that hangs or takes the process down with it must still leave its
    intent behind — otherwise the most interesting failures are the ones that
    write nothing.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    restart_events = [
        e.event
        for e in read_auth_events(event_log)
        if e.event in (RESTART_ATTEMPTED, RESTART_OUTCOME)
    ]
    assert restart_events == [RESTART_ATTEMPTED, RESTART_OUTCOME]


def test_a_raising_restart_still_leaves_an_attempt_and_a_failed_outcome(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """An exception is an outcome too, and a failed one.

    The recorder raises for real; nothing is patched. A restart that blew up
    must not read as a restart that never happened.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux is gone"))

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    assert _events(event_log, RESTART_OUTCOME)[0].succeeded is False


def test_the_wedge_is_observed_before_any_restart_decision(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """What we SAW is recorded first, and separately from what we DID.

    The sighting owes nothing to the restarter's claims about itself, which is
    what lets a series of sightings establish that a wedge outlived its remedy.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    assert read_auth_events(event_log)[0].event == AUTH_FAILURE_OBSERVED


def test_a_check_run_observes_the_wedge_but_attempts_no_restart(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """Observing is not acting. A dry run that saw a wedge really did see it.

    It must therefore record the sighting — and must NOT record an attempt,
    because it did not make one. Writing an attempt here would put phantom
    restarts in the log of a run whose entire promise is that it changed
    nothing.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=False,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    kinds = [e.event for e in read_auth_events(event_log)]
    assert kinds == [AUTH_FAILURE_OBSERVED]


def test_each_wedged_agent_gets_its_own_attempt_id(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """Six agents dying together must be six traceable stories, not one blur.

    Shared ids would let one agent's recovery appear to account for another's,
    which is the failure the 2026-07-18 incident would have been misread as.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=False)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe", "crossref-local"),
        event_log=event_log,
    )

    # Assert
    ids = [e.attempt_id for e in _events(event_log, RESTART_ATTEMPTED)]
    assert len(set(ids)) == 2


def test_an_unresolvable_account_is_recorded_as_null_not_guessed(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """The registry here is empty, so the account is genuinely undeterminable.

    It must read as ``null``. Guessing the host's current account would put a
    plausible value into the field an investigator joins rotations against —
    and a wrong account is worse than a missing one, because it is believed.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )

    # Assert
    observed = _events(event_log, AUTH_FAILURE_OBSERVED)[0]
    assert "account" in observed.raw and observed.account is None


def test_an_unwritable_event_log_does_not_stop_the_restart(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """FAIL-OPEN, proved end to end against a really read-only directory.

    The observability rail must never cost us the recovery it observes. The
    evidence is the recorder: the restart still happened.
    """
    # Arrange
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    recorder = Recorder(ok=True)

    # Act
    try:
        auth_heal_pass(
            apply=True,
            now=NOW,
            history_file=history,
            events_path=events,
            alarm=False,
            restart_fn=recorder,
            capture_fn=lambda: stuck("figrecipe"),
            event_log=readonly / "auth-events.jsonl",
        )
    finally:
        readonly.chmod(0o755)

    # Assert
    assert recorder.names == ["figrecipe"]


def test_a_healthy_fleet_writes_no_auth_events(
    tmp_path: Path, history: Path, events: Path
) -> None:
    """Silence means nothing was seen — the log must not invent activity.

    A rail that writes on every tick regardless would drown the one line that
    matters, and would make "the log is quiet" meaningless.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=True)

    # Act
    auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=dict,
        event_log=event_log,
    )

    # Assert
    assert read_auth_events(event_log) == []
