"""Tests for ``_ratelimit._pass`` — the whole enforcer, end to end, no mocks.

Real on-disk v3 specs, a real temp resume ledger, a real temp sac event log,
real captured banner text, and a real recorder standing in for the ONE
irreversible act. Nothing is patched and nothing is monkeypatched: every
collaborator is a production keyword argument with a real default, and the
tests pass real values into them.

THE POSITIVE CONTROL THIS FILE IS BUILT AROUND
    :func:`test_the_incident_fleet_is_held_before_the_reset` and
    :func:`test_the_incident_fleet_is_resumed_after_the_reset` are ONE
    experiment run twice. Same fleet, same specs, same captured panes, same
    ledger — the ONLY variable is the clock, moved across the reset the
    provider published. Before it the pass holds and touches nothing; after
    it the pass wakes the agent. A green result in the second without a red
    one in the first would prove nothing at all, which is why both legs are
    asserted against the SAME recorder.

    The clock values are the 2026-08-28 incident's own: the wall lifted at
    19:10 UTC and the operator noticed at 20:56 UTC. The "before" leg is set
    at 18:00 UTC, inside the outage.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scitex_agent_container._events import read_events
from scitex_agent_container._ratelimit._pass import resume_pass
from scitex_agent_container._ratelimit._rule import Verdict

from tests.scitex_agent_container._reconcile._fleet import Recorder, write_spec

#: The captured 2026-08-28 banner, in the pane position it really occupied:
#: last conversation line, directly above the prompt box.
WALLED_PANE = "\n".join(
    [
        "● Working on the migration...",
        "  ⎿ You’ve hit your session limit · resets 7:10pm (UTC)",
        "     /usage-credits to finish what you’re working on.",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)
CLEAN_PANE = "\n".join(
    [
        "● Done.",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)

DURING_OUTAGE = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc).timestamp()
AFTER_RESET = datetime(2026, 8, 28, 20, 56, tzinfo=timezone.utc).timestamp()


def _frozen(pane: str) -> tuple[str, str]:
    """Two identical captures — a pane that did not advance."""
    return (pane, pane)


@pytest.fixture()
def fleet(tmp_path):
    """One managed agent whose live pane shows the incident's rate wall."""
    registry = tmp_path / "agents"
    write_spec(registry, "alpha")
    return {
        "specs_dir": registry,
        "history_file": tmp_path / "resume-history.json",
        "events_path": tmp_path / "sac-events.jsonl",
    }


def _run(fleet, *, now, recorder, panes=None, apply=True, **overrides):
    kwargs = dict(fleet)
    kwargs.update(
        {
            "apply": apply,
            "now": now,
            "resume_fn": recorder,
            "capture_fn": lambda: (
                panes if panes is not None else {"alpha": _frozen(WALLED_PANE)}
            ),
        }
    )
    kwargs.update(overrides)
    return resume_pass(**kwargs)


def _verdict_of(outcome, name: str) -> Verdict:
    return next(r.verdict for r in outcome.reports if r.name == name)


# --- THE POSITIVE CONTROL: one experiment, the clock as the only variable ---


def test_the_incident_fleet_is_held_before_the_reset(fleet) -> None:
    # Arrange — 18:00 UTC on 2026-08-28: the session wall is up and lifts at
    # 19:10. This is the RED half of the control. If the pass acted here it
    # would spend a token against a standing limit and lengthen the outage
    # it exists to end.
    recorder = Recorder()
    # Act
    _run(fleet, now=DURING_OUTAGE, recorder=recorder)
    # Assert
    assert recorder.names == []


def test_the_held_agent_is_reported_as_waiting(fleet) -> None:
    # Arrange — the same 18:00 UTC pass. Holding must be a STATED verdict,
    # not an absence: an agent nobody reports on is an agent nobody watches.
    recorder = Recorder()
    # Act
    outcome = _run(fleet, now=DURING_OUTAGE, recorder=recorder)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.WAITING


def test_waiting_exits_zero_because_it_is_not_a_fault(fleet) -> None:
    # Arrange — a wall can stand for hours. Exiting non-zero would leave the
    # systemd unit permanently failed, and a permanently failing unit is one
    # nobody looks at — which would destroy the only signal this job has.
    recorder = Recorder()
    # Act
    outcome = _run(fleet, now=DURING_OUTAGE, recorder=recorder)
    # Assert
    assert outcome.exit_code() == 0


def test_the_incident_fleet_is_resumed_after_the_reset(fleet) -> None:
    # Arrange — 20:56 UTC, the minute the operator had to ask. Same fleet,
    # same specs, same captured panes, same ledger as the two legs above;
    # ONLY the clock moved, past the 19:10 reset the provider printed. This
    # is the GREEN half, and it is the claim that this enforcer would have
    # recovered the fleet 1h46m before a human did.
    recorder = Recorder()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert recorder.names == ["alpha"]


def test_the_resumed_agent_is_reported_as_resumed(fleet) -> None:
    # Arrange — the same 20:56 UTC pass, with a resume that succeeded.
    recorder = Recorder()
    # Act
    outcome = _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.RESUMED


# --- the read-only default -------------------------------------------------


def test_the_default_pass_wakes_nothing(fleet) -> None:
    # Arrange — detection is read-only by default and the nudge is the only
    # mutation in the whole flow, so a pass without --apply must be provably
    # inert even when the wall has lifted.
    recorder = Recorder()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder, apply=False)
    # Assert
    assert recorder.names == []


def test_a_check_pass_still_names_the_owed_resume(fleet) -> None:
    # Arrange — inert is not the same as silent. A dry run must say what it
    # WOULD have done, or it cannot be used to preview anything.
    recorder = Recorder()
    # Act
    outcome = _run(fleet, now=AFTER_RESET, recorder=recorder, apply=False)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.WOULD_RESUME


# --- the debounce, on this enforcer's OWN ledger ----------------------------


def test_a_second_pass_inside_the_debounce_holds(fleet) -> None:
    # Arrange — the timer ticks every 5 minutes and the per-agent debounce is
    # 30, so a woken agent is cooling down for its next five ticks. Waking it
    # again each tick is the restart loop that is worse than the outage.
    recorder = Recorder()
    _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Act
    _run(fleet, now=AFTER_RESET + 60, recorder=recorder)
    # Assert
    assert recorder.names == ["alpha"]


def test_the_resume_is_persisted_to_the_ledger(fleet) -> None:
    # Arrange — the debounce above is only real if it survives the process.
    # Each pass is a separate short-lived CLI invocation, so an in-memory
    # budget would reset every five minutes and enforce nothing.
    recorder = Recorder()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert "alpha" in json.loads(fleet["history_file"].read_text())


def test_an_unreadable_ledger_refuses_to_wake_anything(fleet, tmp_path) -> None:
    # Arrange — a budget we cannot read is not a budget. Treating an
    # unreadable ledger as an empty one would silently disarm every rate
    # limit on a permission error, which is the loudest possible way to be
    # quiet.
    recorder = Recorder()
    blocked = tmp_path / "nope"
    blocked.mkdir()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder, history_file=blocked)
    # Assert
    assert recorder.names == []


# --- the failure path: an agent we could not wake must be VISIBLE -----------


def test_an_unprovable_nudge_is_a_failure_not_a_win(fleet) -> None:
    # Arrange — the delivery layer could not prove the payload left the
    # compose box. A reviver that reports success it did not achieve leaves
    # the operator believing the fleet recovered; that is the outage with an
    # extra step.
    recorder = Recorder(ok=False)
    # Act
    outcome = _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.FAILED


def test_a_failed_resume_reaches_the_exit_code(fleet) -> None:
    # Arrange — the exit code is this job's REAL reader: a non-zero exit
    # fails the systemd unit, which is visible in `list-units --failed`, in
    # the journal, and to the ecosystem supervisor. The event log has no
    # production reader, so the exit code has to carry it.
    recorder = Recorder(ok=False)
    # Act
    outcome = _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert outcome.exit_code() == 1


def test_a_failed_resume_is_recorded_as_degraded(fleet) -> None:
    # Arrange — an enforcer that gives up SILENTLY is the original bug with
    # extra steps. The record is written to a real temp event log and read
    # back with the production reader.
    recorder = Recorder(ok=False)
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder)
    # Assert
    assert any(
        e.subject == "alpha" and e.event == "subject-degraded"
        for e in read_events(fleet["events_path"])
    )


def test_a_wall_we_cannot_time_is_reported_not_guessed(fleet) -> None:
    # Arrange — a real banner whose reset clause does not parse. Nothing here
    # can resolve it, so it must reach a human rather than be waited out on
    # an invented deadline.
    recorder = Recorder()
    untimed = "\n".join(["  ⎿ You've hit your session limit", "❯ "])
    # Act
    outcome = _run(
        fleet,
        now=AFTER_RESET,
        recorder=recorder,
        panes={"alpha": _frozen(untimed)},
    )
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.RESET_UNKNOWN


def test_an_untimed_wall_exits_two_as_indeterminate(fleet) -> None:
    # Arrange — the same pass. "We saw a wall and cannot say when it lifts"
    # is an unresolved reading, not a known-bad outcome, so it groups with
    # the blind cases rather than the failed ones.
    recorder = Recorder()
    untimed = "\n".join(["  ⎿ You've hit your session limit", "❯ "])
    # Act
    outcome = _run(
        fleet,
        now=AFTER_RESET,
        recorder=recorder,
        panes={"alpha": _frozen(untimed)},
    )
    # Assert
    assert outcome.exit_code() == 2


# --- who this pass is NOT for, at pass level --------------------------------


def test_an_agent_with_no_session_is_left_to_reconcile(fleet) -> None:
    # Arrange — the agent is registered but has no live pane, so it never
    # appears in the capture. That is fleet-reconcile's half of the fleet.
    recorder = Recorder()
    # Act
    outcome = _run(fleet, now=AFTER_RESET, recorder=recorder, panes={})
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.NO_SESSION


def test_a_healthy_pane_is_never_nudged(fleet) -> None:
    # Arrange — a live agent with no wall at all. This is the majority of
    # every pass, and touching one of these would interrupt working agents
    # every five minutes forever.
    recorder = Recorder()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder, panes={"alpha": _frozen(CLEAN_PANE)})
    # Assert
    assert recorder.names == []


def test_an_unmanaged_agent_is_never_nudged(fleet, tmp_path) -> None:
    # Arrange — restart.policy "never" means sac never promised to keep this
    # agent running, so it is not this enforcer's to wake even behind a
    # lifted wall. Same opt-in as fleet-reconcile.
    recorder = Recorder()
    write_spec(fleet["specs_dir"], "beta", policy="never")
    # Act
    outcome = _run(
        fleet,
        now=AFTER_RESET,
        recorder=recorder,
        panes={"beta": _frozen(WALLED_PANE)},
    )
    # Assert
    assert _verdict_of(outcome, "beta") is Verdict.NOT_MANAGED


# --- who watches the watcher ------------------------------------------------


def test_every_pass_records_that_it_ran(fleet) -> None:
    # Arrange — a pass that found nothing to do writes the most important
    # record there is. fleet-reconcile's timer sat in systemd's `elapsed`
    # state for NINE DAYS reporting `active` and firing never, and nothing
    # noticed, because a silent enforcer and a satisfied one look identical.
    recorder = Recorder()
    # Act
    _run(fleet, now=AFTER_RESET, recorder=recorder, panes={"alpha": _frozen(CLEAN_PANE)})
    # Assert
    assert any(
        e.event == "pass-completed" and e.subsystem == "rate-limit-resume"
        for e in read_events(fleet["events_path"])
    )


# --- blindness: it may cost inaction, it may NOT look like a clean pass ------


class _Blind:
    """A capture seam that FAILS, the way a broken tmux read really does.

    Not a mock — a plain callable with the production signature, which is the
    only thing the pass ever asks of it.
    """

    def __call__(self):
        raise OSError("tmux: connection refused")


def test_a_failed_capture_is_not_an_empty_fleet(fleet) -> None:
    # Arrange — the pane read itself blew up. Treating that as "no agent is
    # walled" would report a healthy fleet on the strength of having seen
    # nothing, which is the instrument failure every enforcer in this package
    # is built to refuse.
    recorder = Recorder()
    # Act
    outcome = resume_pass(
        **fleet,
        apply=True,
        now=AFTER_RESET,
        resume_fn=recorder,
        capture_fn=_Blind(),
    )
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.UNREADABLE


def test_a_blind_pass_cannot_exit_clean(fleet) -> None:
    # Arrange — the same failed read. The exit code is this job's real reader,
    # so a pass that learned nothing must not let the systemd unit log a
    # healthy tick.
    recorder = Recorder()
    # Act
    outcome = resume_pass(
        **fleet,
        apply=True,
        now=AFTER_RESET,
        resume_fn=recorder,
        capture_fn=_Blind(),
    )
    # Assert
    assert outcome.exit_code() == 2


def test_a_blind_pass_wakes_nothing(fleet) -> None:
    # Arrange — the same failed read. Blindness here may cost INACTION; it must
    # never authorise action, because a nudge sent on evidence we do not have
    # would interrupt an agent that was working.
    recorder = Recorder()
    # Act
    resume_pass(
        **fleet,
        apply=True,
        now=AFTER_RESET,
        resume_fn=recorder,
        capture_fn=_Blind(),
    )
    # Assert
    assert recorder.names == []
