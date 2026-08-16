"""Tests for ``_reconcile._rule`` — WHICH dead agents may sac resurrect?

The rule is pure, so every leg is driven by passing facts in. No mocks and
nothing to mock: no clock, no tmux, no database.

The behaviours that matter, in the order they matter:

* a GHOST ACTIVE ROW with no session is a corpse → RESTART. This is the
  exact state 33 agents were left in when an OAuth rotation killed them,
  and the leg that proves this command would have recovered the fleet.
* a DELIBERATE stop is never undone. The operator's intent is sacred, and
  an enforcer that second-guesses it is worse than no enforcer.
* "I could not look" is never "nothing is there" — the leg that stops this
  command restarting the whole fleet the first time it runs somewhere blind.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._reconcile._rule import Verdict, decide


def _decide(**overrides):
    """Decide for a managed agent that a probe FOUND NO SESSION for.

    That is the interesting half of the table — every leg below differs
    only in what the ``instances`` row says happened to it.
    """
    facts = {
        "name": "alpha",
        "policy": "on-failure",
        "probe_ran": True,
        "session_present": False,
        "row": None,
        "local_host": "host-a",
    }
    facts.update(overrides)
    return decide(**facts)


def _row(**overrides) -> dict:
    """A plain ``instances`` row, shaped exactly as sqlite3 hands one back."""
    row = {
        "name": "alpha",
        "host": "host-a",
        "remote": 0,
        "ended_at": None,
        "exit_reason": None,
    }
    row.update(overrides)
    return row


# --- the PIN: ownership is asked before liveness -----------------------------


def _pinned_elsewhere_corpse():
    """A PERFECT local corpse whose spec pins it somewhere else.

    Deliberately every signal says "restart me": the row's host matches the
    local host and ``ended_at`` is None. Only the pin dissents. That is the
    exact production shape — scitex-hub was pinned to scitex-nas-03, had never
    managed to RUN there, so its row said compute-04, the old guard read
    row_host == local_host as "mine", and every 5-minute pass restarted it on
    the wrong machine while rewriting the row to agree.
    """
    return _decide(
        row=_row(host="host-a", ended_at=None),
        local_host="host-a",
        declared_host_is_local=False,
    )


def test_agent_pinned_to_another_host_is_skipped():
    # Arrange
    # Act
    decision = _pinned_elsewhere_corpse()
    # Assert
    assert decision.verdict is Verdict.SKIPPED


def test_agent_pinned_to_another_host_says_why():
    # Arrange
    # Act
    decision = _pinned_elsewhere_corpse()
    # Assert
    assert decision.reason == "pinned-elsewhere"


def test_a_pin_naming_this_host_still_restarts():
    # Arrange
    row = _row(ended_at=None)
    # Act
    decision = _decide(row=row, declared_host_is_local=True)
    # Assert
    assert decision.verdict is Verdict.RESTART


def test_an_unresolvable_pin_is_unknown_not_a_refusal():
    """``None`` is not ``False``.

    A pin we could not resolve must leave the previous behaviour untouched.
    Inventing a refusal from a failed lookup would stop the enforcer
    restarting the WHOLE fleet the moment the peer registry became unreadable.
    """
    # Arrange
    row = _row(ended_at=None)
    # Act
    decision = _decide(row=row, declared_host_is_local=None)
    # Assert
    assert decision.verdict is Verdict.RESTART


def test_callers_that_omit_the_pin_are_unaffected():
    # Arrange
    row = _row(ended_at=None)
    # Act
    decision = _decide(row=row)
    # Assert
    assert decision.verdict is Verdict.RESTART


# --- the promise: is this agent even ours to keep alive? --------------------


@pytest.mark.parametrize("policy", ["never", "", "unknown-policy"])
def test_unmanaged_policy_is_never_touched(policy):
    # Arrange — RestartSpec.policy DEFAULTS to "never", so a spec that
    # merely omits a restart: block must never be resurrected.
    # Act
    decision = _decide(policy=policy, row=_row())
    # Assert
    assert decision.verdict is Verdict.NOT_MANAGED


@pytest.mark.parametrize("policy", ["always", "on-failure"])
def test_managed_policies_are_enforced(policy):
    # Arrange — a corpse under a policy that promised to keep it running.
    # Act
    decision = _decide(policy=policy, row=_row())
    # Assert
    assert decision.verdict is Verdict.RESTART


# --- tmux is the fact, and a non-observation is not a fact ------------------


def test_unreadable_tmux_is_unknown_not_dead():
    # Arrange — probe_ran=None means the tmux read was not a sensor from
    # here (a container's tmux is a different namespace and SUCCEEDS while
    # reporting an empty fleet). Inferring death here would restart all 93.
    # Act
    decision = _decide(probe_ran=None, session_present=None, row=_row())
    # Assert
    assert decision.verdict is Verdict.UNKNOWN


def test_unreadable_tmux_says_it_could_not_look():
    # Arrange — the reason must not read as an observation of absence.
    # Act
    decision = _decide(probe_ran=None, session_present=None, row=_row())
    # Assert
    assert decision.reason == "could-not-look"


def test_live_session_is_left_alone():
    # Arrange — a session EXISTS. Alive, so hands off: restarting a live
    # agent would destroy its context, and a wedged one is auth-heal's job.
    # Act
    decision = _decide(session_present=True, row=_row(ended_at="2026-07-16T00:00:00Z"))
    # Assert
    assert decision.verdict is Verdict.OK


def test_live_session_outranks_a_crashed_row():
    # Arrange — the row says crashed but tmux says the session is THERE.
    # tmux is the fact; the registry is a hypothesis.
    # Act
    decision = _decide(
        session_present=True,
        row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason="crashed"),
    )
    # Assert
    assert decision.verdict is Verdict.OK


# --- THE CORPSE SIGNATURE: tonight's 33 dead agents -------------------------


def test_ghost_active_row_without_session_is_restarted():
    # Arrange — no tmux session, yet the row still claims ACTIVE
    # (ended_at IS NULL). Nothing recorded an end, so nobody ended it: it
    # DIED. This is exactly the state the OAuth rotation left 33 agents in.
    # Act
    decision = _decide(row=_row(ended_at=None))
    # Assert
    assert decision.verdict is Verdict.RESTART


def test_ghost_active_row_names_the_corpse_signature():
    # Arrange — the reason is what the operator reads in the cron log.
    # Act
    decision = _decide(row=_row(ended_at=None))
    # Assert
    assert decision.reason == "ghost-active-row"


@pytest.mark.parametrize(
    "reason", ["pid_absent_at_sweep", "crashed", "reboot-swept"]
)
def test_unexpected_exit_reason_is_restarted(reason):
    # Arrange — the reaper writes 'pid_absent_at_sweep' (and wrote 'crashed'
    # before 2026-08-12); the reboot sweep wrote 'reboot-swept'. None of them
    # is a human deciding to stop the agent.
    # Act
    decision = _decide(row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason=reason))
    # Assert
    assert decision.verdict is Verdict.RESTART


def test_legacy_crashed_rows_are_still_recognised_as_corpses():
    """Rows written before the rename must not become unclassifiable.

    Live databases hold them — eleven on the fleet host the day this landed.
    Dropping the old spelling would send every one of them down the
    "an exit_reason this rule does not know" path, which refuses to act, so
    real corpses would silently stop being recoverable.
    """
    # Arrange
    row = _row(ended_at="2026-07-16T00:00:00Z", exit_reason="crashed")
    # Act
    decision = _decide(row=row)
    # Assert
    assert decision.verdict is Verdict.RESTART


# --- THE OPERATOR'S INTENT IS SACRED ---------------------------------------


@pytest.mark.parametrize("reason", ["stopped", "deleted"])
def test_deliberate_exit_reason_is_never_restarted(reason):
    # Arrange — `sac agents stop` writes 'stopped'; delete writes 'deleted'.
    # A reconciler that undoes these is a reconciler nobody can turn off.
    # Act
    decision = _decide(row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason=reason))
    # Assert
    assert decision.verdict is Verdict.SKIPPED


@pytest.mark.parametrize("reason", ["stopped", "deleted"])
def test_deliberate_skip_is_never_silent(reason):
    # Arrange — a skip must SAY it skipped and why; a silent skip is
    # indistinguishable from a bug.
    # Act
    decision = _decide(row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason=reason))
    # Assert
    assert decision.reason == "deliberate"


# --- corpses that are not ours to raise ------------------------------------


def test_agent_that_never_started_is_skipped():
    # Arrange — a spec with NO instances row has never run here. Starting
    # it would be a start nobody asked for, not a restart — and doing that
    # for every unstarted spec at once is a fleet storm.
    # Act
    decision = _decide(row=None)
    # Assert
    assert decision.reason == "never-started"


def test_remote_agent_is_not_restarted_locally():
    # Arrange — remote=1: the agent landed on ANOTHER host. This pass reads
    # only the LOCAL tmux, so its absence here is not evidence of death and
    # a local restart would DUPLICATE a live remote agent.
    # Act
    decision = _decide(row=_row(remote=1))
    # Assert
    assert decision.verdict is Verdict.SKIPPED


def test_row_from_another_host_is_skipped():
    # Arrange — the row was written on host-b; we are host-a. Its tmux is
    # not ours to read.
    # Act
    decision = _decide(row=_row(host="host-b"), local_host="host-a")
    # Assert
    assert decision.reason == "other-host"


@pytest.mark.parametrize("reason", ["superseded", "stale-cleared"])
def test_bookkeeping_exit_reason_is_skipped(reason):
    # Arrange — sac's own internal bookkeeping, not a statement that the
    # agent died: 'superseded' means a NEWER row took over.
    # Act
    decision = _decide(row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason=reason))
    # Assert
    assert decision.verdict is Verdict.SKIPPED


def test_unrecognised_exit_reason_is_not_guessed():
    # Arrange — an exit_reason this rule has never seen. Guessing that it
    # means death is how an enforcer restarts something it should not.
    # Act
    decision = _decide(
        row=_row(ended_at="2026-07-16T00:00:00Z", exit_reason="who-knows")
    )
    # Assert
    assert decision.verdict is Verdict.SKIPPED


# --- every verdict carries its evidence ------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"ended_at": None},
        {"ended_at": "2026-07-16T00:00:00Z", "exit_reason": "stopped"},
        {"ended_at": "2026-07-16T00:00:00Z", "exit_reason": "crashed"},
    ],
)
def test_every_decision_states_a_detail(row):
    # Arrange — no leg may reach the operator without saying why.
    # Act
    decision = _decide(row=row)
    # Assert
    assert decision.detail.strip()
