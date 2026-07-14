"""The ternary liveness verdict — ALIVE / DEAD / UNKNOWN, and what it authorises.

NO MOCKS (repo doctrine). :mod:`._verdict` is a pure decision rule, so these
drive it with real ``Signal`` values — the exact objects the real resolvers
emit. The resolvers' own IO is covered in ``test__verdict_resolve.py`` against
real files, real processes and a real tmux socket.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    SOURCE_DELIVERY,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    UNKNOWN,
    Signal,
    decide,
)


@pytest.fixture
def writer_signals():
    """The ``scitex-writer`` refutation, as real signals.

    A peer refuted an earlier ``pid AND port AND session_id`` predicate with a
    real agent that carried a stale ``startup_failed`` status, an unbound port
    and a null session_id — and was ANSWERING messages. Every "dead" signal was
    a PROXY; the one that observed the agent itself said alive.
    """
    return [
        Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber"),
        Signal(SOURCE_PROCESS, DEAD, "no apptainer pid"),
        Signal(SOURCE_REGISTRY, DEAD, "recorded pid is reaped"),
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is stale"),
    ]


@pytest.fixture
def all_unknown_signals():
    """Four "I could not look"s. The ``grant`` shape, as measured 2026-07-14."""
    return [
        Signal(SOURCE_DELIVERY, UNKNOWN, "could not ask the broker"),
        Signal(SOURCE_PROCESS, UNKNOWN, "tmux probe FAILED"),
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is 5086s stale"),
        Signal(SOURCE_REGISTRY, UNKNOWN, "active row records pid=0"),
    ]


@pytest.fixture
def corroborated_dead_signals():
    """Two INDEPENDENT sources that each positively observed absence."""
    return [
        Signal(SOURCE_PROCESS, DEAD, "tmux probe succeeded; no session"),
        Signal(SOURCE_REGISTRY, DEAD, "recorded pid=1234 is REAPED"),
        Signal(SOURCE_DELIVERY, UNKNOWN, "0 subscribers — adapter detached"),
    ]


# --------------------------------------------------------------------------
# A verdict is never a bool.
# --------------------------------------------------------------------------


def test_signal_rejects_a_non_ternary_verdict():
    """A bool cannot express "I could not tell", so the type refuses it."""
    # Arrange
    bogus = "running"
    # Act
    # (constructing the Signal IS the act under test — it must refuse.)
    # Assert
    with pytest.raises(ValueError, match="never a bool"):
        Signal(SOURCE_PROCESS, bogus, "not a ternary verdict")


def test_no_signals_at_all_is_unknown():
    """The bug, stated as a test: 'we gathered nothing' must NOT mean 'dead'."""
    # Arrange
    signals: list[Signal] = []
    # Act
    verdict = decide("ghost", signals)
    # Assert
    assert verdict.verdict == UNKNOWN


def test_no_signals_at_all_authorises_nothing_destructive():
    # Arrange
    signals: list[Signal] = []
    # Act
    verdict = decide("ghost", signals)
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# Rule 1: positive evidence of life is never overruled.
# --------------------------------------------------------------------------


def test_one_alive_signal_beats_every_dead_proxy(writer_signals):
    # Arrange
    agent = "scitex-writer"
    # Act
    verdict = decide(agent, writer_signals)
    # Assert
    assert verdict.verdict == ALIVE


def test_a_live_agent_may_never_be_destroyed_however_many_proxies_dissent(
    writer_signals,
):
    # Arrange
    agent = "scitex-writer"
    # Act
    verdict = decide(agent, writer_signals)
    # Assert
    assert verdict.may_destroy is False


def test_destroy_veto_names_the_agent_as_alive(writer_signals):
    # Arrange
    agent = "scitex-writer"
    # Act
    verdict = decide(agent, writer_signals)
    # Assert
    assert "refusing to destroy a live agent" in verdict.destroy_veto_reason


def test_alive_verdict_names_the_signal_that_observed_life():
    # Arrange
    signals = [Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber(s)")]
    # Act
    verdict = decide("grant", signals)
    # Assert
    assert verdict.render().startswith("ALIVE (delivery: 1 live inbox subscriber")


# --------------------------------------------------------------------------
# Rule 2: DEAD needs POSITIVE evidence.
# --------------------------------------------------------------------------


def test_unknown_signals_alone_never_produce_dead(all_unknown_signals):
    """Four 'I could not look's still add up to 'I do not know'."""
    # Arrange
    agent = "dotfiles"
    # Act
    verdict = decide(agent, all_unknown_signals)
    # Assert
    assert verdict.verdict == UNKNOWN


def test_unknown_verdict_authorises_nothing_destructive(all_unknown_signals):
    # Arrange
    agent = "dotfiles"
    # Act
    verdict = decide(agent, all_unknown_signals)
    # Assert
    assert verdict.may_destroy is False


def test_unknown_veto_says_a_failed_probe_is_not_evidence_of_death(
    all_unknown_signals,
):
    # Arrange
    agent = "dotfiles"
    # Act
    verdict = decide(agent, all_unknown_signals)
    # Assert
    assert "not evidence of death" in verdict.destroy_veto_reason


def test_a_single_dead_signal_still_yields_a_dead_verdict():
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "tmux probe succeeded; no session"),
        Signal(SOURCE_DELIVERY, UNKNOWN, "no listen to ask"),
    ]
    # Act
    verdict = decide("scitex-dev", signals)
    # Assert
    assert verdict.verdict == DEAD


def test_a_single_dead_witness_does_not_authorise_destruction():
    """DEAD is a report. On one witness it is not a licence to destroy."""
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "tmux probe succeeded; no session"),
        Signal(SOURCE_DELIVERY, UNKNOWN, "no listen to ask"),
    ]
    # Act
    verdict = decide("scitex-dev", signals)
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# Rule 3: only a CORROBORATED dead may authorise a destructive remedy.
# --------------------------------------------------------------------------


def test_two_independent_dead_sources_authorise_destruction(
    corroborated_dead_signals,
):
    # Arrange
    agent = "scitex-dev"
    # Act
    verdict = decide(agent, corroborated_dead_signals)
    # Assert
    assert verdict.may_destroy is True


def test_an_authorised_destruction_has_no_veto_reason(corroborated_dead_signals):
    # Arrange
    agent = "scitex-dev"
    # Act
    verdict = decide(agent, corroborated_dead_signals)
    # Assert
    assert verdict.destroy_veto_reason == ""


def test_the_same_source_twice_is_not_corroboration():
    """Corroboration means INDEPENDENT sources, not one signal counted twice."""
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "no session"),
        Signal(SOURCE_PROCESS, DEAD, "no pane pid either"),
    ]
    # Act
    verdict = decide("x", signals)
    # Assert
    assert verdict.may_destroy is False


def test_one_dissenting_alive_vetoes_destruction_outright():
    """The asymmetry is the point: a false DEAD destroys, a false ALIVE reports."""
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "no session"),
        Signal(SOURCE_REGISTRY, DEAD, "pid reaped"),
        Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber"),
    ]
    # Act
    verdict = decide("x", signals)
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# Rule 4: report the verdict AND its evidence.
# --------------------------------------------------------------------------


def test_render_carries_the_reason_not_just_the_verdict():
    """``running | pid=None`` teaches an operator nothing. This must teach them."""
    # Arrange
    signals = [
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is 5086s stale"),
        Signal(SOURCE_REGISTRY, UNKNOWN, "active row records pid=0"),
    ]
    # Act
    rendered = decide("grant", signals).render()
    # Assert
    assert "beat is 5086s stale" in rendered


def test_render_surfaces_the_unfalsifiable_pid_zero_as_evidence():
    # Arrange
    signals = [Signal(SOURCE_REGISTRY, UNKNOWN, "active row records pid=0")]
    # Act
    rendered = decide("grant", signals).render()
    # Assert
    assert "pid=0" in rendered


def test_render_shows_dissenting_signals_rather_than_hiding_them():
    # Arrange
    signals = [
        Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber"),
        Signal(SOURCE_PROCESS, DEAD, "no session"),
    ]
    # Act
    rendered = decide("grant", signals).render()
    # Assert
    assert "process[dead]" in rendered


def test_to_dict_always_carries_an_evidence_key():
    """A consumer must never have to guess whether we actually looked."""
    # Arrange
    signals: list[Signal] = []
    # Act
    payload = decide("x", signals).to_dict()
    # Assert
    assert payload["evidence"] == []
