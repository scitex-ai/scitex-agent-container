"""The aggregation rule — and the MUTATION PROOF that these gates can go RED.

A gate nobody has proven RED is a hope with YAML around it. So this suite does
not merely assert that :func:`assess` handles ``None`` correctly; it carries a
NAIVE implementation — the one this card exists to replace, which collapses
"could not determine" into a pole — and asserts on the SAME inputs that the naive
one gets them WRONG. Each ``None``-handling gate is therefore demonstrably
capable of failing, rather than passing because it asks nothing.

No mocks: an :class:`AgentState` is a real frozen dataclass and the rule under
test is pure, so every case here runs the production code path on real values.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._agentstate import (
    LOAD_BEARING,
    AgentState,
    assess,
    spec_for,
)

#: The values that are healthy on every load-bearing signal, built FROM the spec
#: — so adding a load-bearing criterion cannot leave this suite quietly testing a
#: smaller set than production folds.
HEALTHY = {name: spec_for(name).healthy for name in LOAD_BEARING}


def healthy_state(agent: str = "alpha", **overrides) -> AgentState:
    """A state observed healthy on every load-bearing signal."""
    values = dict(HEALTHY)
    values.update(overrides)
    return AgentState(agent=agent, **values)


def naive_assess(state: AgentState) -> bool:
    """THE BUG, written out: treat an unread signal as fine, return a BOOL.

    Not a strawman — this is the shape the fleet actually shipped: a detector
    that computed three verdicts and returned ``list[str]``, an exit-code
    function ending in a bare ``return 0``, an enumeration whose emptiness read
    as a healthy fleet. All of them collapse ``None`` into the OK pole.
    """
    return all(
        getattr(state, name) is not (not spec_for(name).healthy)
        for name in LOAD_BEARING
    )


# ---------------------------------------------------------------------------
# True — every load-bearing signal observed healthy.
# ---------------------------------------------------------------------------


def test_all_load_bearing_signals_healthy_yields_true():
    # Arrange
    state = healthy_state()
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is True


def test_all_load_bearing_signals_healthy_exits_zero():
    # Arrange
    state = healthy_state()
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.exit_code() == 0


def test_a_true_verdict_leaves_nothing_unresolved():
    # Arrange
    state = healthy_state()
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.unresolved == ()


def test_an_unhealthy_non_load_bearing_signal_cannot_refute():
    """Evidence signals never flip the verdict — that is what load-bearing means.

    Load-bearingness is chosen by false-red cost: zero inbox subscribers means a
    DETACHED adapter (agents with 0 have answered messages the same minute) and a
    stale heartbeat has a shared writer. If either could refute, this verdict
    would flag a working fleet and promptly be turned off.
    """
    # Arrange
    state = healthy_state(is_inbox_reachable=False, is_heartbeat_fresh=False)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is True


# ---------------------------------------------------------------------------
# None — a load-bearing signal unread. THE CENTRAL GATE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signal", LOAD_BEARING)
def test_any_unread_load_bearing_signal_yields_unknown(signal):
    """Parametrised over the SPEC, so a signal added later is covered that day."""
    # Arrange
    state = healthy_state(**{signal: None})
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


@pytest.mark.parametrize("signal", LOAD_BEARING)
def test_any_unread_load_bearing_signal_exits_two(signal):
    # Arrange
    state = healthy_state(**{signal: None})
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.exit_code() == 2


@pytest.mark.parametrize("signal", LOAD_BEARING)
def test_an_unknown_verdict_names_the_signal_it_could_not_read(signal):
    """An UNKNOWN that will not say WHICH signal is unread is unactionable."""
    # Arrange
    state = healthy_state(**{signal: None})
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.unresolved == (signal,)


@pytest.mark.parametrize("signal", LOAD_BEARING)
def test_the_naive_rule_calls_an_unread_signal_healthy(signal):
    """MUTATION PROOF, half 1: the pre-fix logic returns True on this same state.

    Its partner below runs :func:`assess` on the identical input and gets None.
    Two implementations, one input, opposite answers — which is what makes the
    gates above real gates instead of restatements.
    """
    # Arrange
    state = healthy_state(**{signal: None})
    # Act
    naive = naive_assess(state)
    # Assert
    assert naive is True


@pytest.mark.parametrize("signal", LOAD_BEARING)
def test_assess_disagrees_with_the_naive_rule_on_an_unread_signal(signal):
    """MUTATION PROOF, half 2: same input, and the real rule refuses to guess."""
    # Arrange
    state = healthy_state(**{signal: None})
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is not naive_assess(state)


def test_unknown_outranks_a_non_decisive_refutation():
    """A refutation from PARTIAL information is a guess, so None wins.

    Order matters because negative verdicts get ACTED on: the remedy a caller
    reaches for (restart, --force, kill) destroys the thing it misdiagnosed.
    """
    # Arrange
    state = healthy_state(is_login_required=True, is_tmux_live=None)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


def test_every_unresolved_signal_is_named_not_only_the_first():
    # Arrange
    state = healthy_state(is_tmux_live=None, is_login_required=None)
    # Act
    verdict = assess(state)
    # Assert
    assert set(verdict.unresolved) == {"is_tmux_live", "is_login_required"}


def test_an_unknown_carries_the_per_signal_reason_into_the_verdict():
    """The reason map distinguishes 'unreadable pane' from 'nobody looked'."""
    # Arrange
    state = AgentState(
        agent="alpha",
        **{**HEALTHY, "is_tmux_live": None},
        reasons={"is_tmux_live": "the host tmux socket is in another namespace"},
    )
    # Act
    verdict = assess(state)
    # Assert
    assert "another namespace" in verdict.reason


# ---------------------------------------------------------------------------
# False — a refutation with COMPLETE information.
# ---------------------------------------------------------------------------


def test_a_refutation_with_every_signal_read_yields_false():
    # Arrange
    state = healthy_state(is_login_required=True)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is False


def test_a_refutation_with_every_signal_read_exits_one():
    # Arrange
    state = healthy_state(is_login_required=True)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.exit_code() == 1


def test_a_false_verdict_names_the_refuting_signal():
    # Arrange
    state = healthy_state(is_login_required=True)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.deciding == ("is_login_required",)


def test_the_healthy_pole_of_is_login_required_is_false():
    """The SPEC decides which pole is good, not the reader's guess at the name.

    Getting this backwards would invert the fleet's login verdict — the single
    signal the whole incident turns on.
    """
    # Arrange
    spec = spec_for("is_login_required")
    # Act
    healthy = spec.healthy
    # Assert
    assert healthy is False


def test_a_login_required_agent_is_refuted_not_approved():
    # Arrange
    state = healthy_state(is_login_required=True)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is False


# ---------------------------------------------------------------------------
# The DECISIVE amendment.
# ---------------------------------------------------------------------------


def test_a_decisive_refutation_short_circuits_past_unread_signals():
    """``is_process_alive=False`` convicts even with other signals unread.

    Without this, ANY single unreadable signal renders UNKNOWN and blocks repair
    of a genuinely dead agent — and on a real fleet something is always
    unreadable somewhere, so the pure rule alone degrades into a system that can
    observe problems and never fix them.
    """
    # Arrange
    state = healthy_state(
        is_process_alive=False, is_tmux_live=None, is_login_required=None
    )
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is False


def test_a_decisive_short_circuit_names_the_signal_that_decided():
    # Arrange
    state = healthy_state(is_process_alive=False, is_tmux_live=None)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.decided_by == "is_process_alive"


def test_a_decisive_verdict_still_reports_what_it_never_read():
    """ "Dead, and here is what we never checked" is a different claim from "dead".

    The short-circuit may convict; it may not pretend the reading was complete.
    """
    # Arrange
    state = healthy_state(is_process_alive=False, is_tmux_live=None)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.unresolved == ("is_tmux_live",)


def test_a_non_decisive_refutation_does_not_short_circuit():
    """The CONTROL that keeps the decisive gate from being trivially true.

    If ANY refutation short-circuited, the tests above would pass without the
    amendment existing at all. ``is_login_required`` refutes and must still lose
    to an unread signal.
    """
    # Arrange
    state = healthy_state(is_login_required=True, is_process_alive=None)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


def test_a_decisive_signal_at_its_healthy_pole_proves_nothing_alone():
    # Arrange
    state = healthy_state(is_process_alive=True, is_tmux_live=None)
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


# ---------------------------------------------------------------------------
# The missing agent — silence must become a value.
# ---------------------------------------------------------------------------


def test_a_missing_agent_has_every_signal_none():
    """The scitex-hub failure: an agent absent from an enumeration had NO ROW.

    ``auth-status`` enumerates only RUNNING TUI agents, so a wedged agent
    produced no row and the silence read as fine.
    """
    # Arrange
    state = AgentState.unknown("scitex-hub", "no live tui- session on this host")
    # Act
    signals = state.signals()
    # Assert
    assert set(signals.values()) == {None}


def test_a_missing_agent_still_renders_the_full_signal_set():
    """The shape is FIXED — a missing agent is a full row, not a short one."""
    # Arrange
    state = AgentState.unknown("scitex-hub", "no live tui- session")
    # Act
    signals = state.signals()
    # Assert
    assert len(signals) == 9


def test_a_missing_agent_assesses_unknown():
    # Arrange
    state = AgentState.unknown("scitex-hub", "no live tui- session")
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


def test_a_missing_agent_exits_two():
    # Arrange
    state = AgentState.unknown("scitex-hub", "no live tui- session")
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.exit_code() == 2


def test_a_missing_agent_reports_why_it_could_not_be_read():
    # Arrange
    state = AgentState.unknown("scitex-hub", "no live tui- session on this host")
    # Act
    verdict = assess(state)
    # Assert
    assert "no live tui- session" in verdict.reason


def test_the_naive_rule_calls_an_agent_it_never_saw_healthy():
    """MUTATION PROOF for the missing-agent gate: all-None is the worst case.

    Nothing whatsoever was observed, and the pre-fix logic returns True for it.
    """
    # Arrange
    state = AgentState.unknown("scitex-hub", "never read")
    # Act
    naive = naive_assess(state)
    # Assert
    assert naive is True


def test_a_default_constructed_state_cannot_claim_health():
    """The type makes the SAFE answer the default: observe something first."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    verdict = assess(state)
    # Assert
    assert verdict.verdict is None


# ---------------------------------------------------------------------------
# Exit codes — the summary, and only the summary.
# ---------------------------------------------------------------------------


def test_assess_is_pure_and_repeatable():
    """Same state, same answer, forever — no clock, no environment, no IO."""
    # Arrange
    state = healthy_state(is_tmux_live=None)
    # Act
    first = assess(state).to_dict()
    # Assert
    assert first == assess(state).to_dict()


# EOF
