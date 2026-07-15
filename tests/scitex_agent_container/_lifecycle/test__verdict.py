"""The ternary liveness verdict — ALIVE / DEAD / UNKNOWN, and what it authorises.

NO MOCKS (repo doctrine). :mod:`._verdict` is a pure decision rule, so these
drive it with real ``Signal`` values — the exact objects the real resolvers
emit. The resolvers' own IO is covered in ``test__verdict_resolve.py`` against
real files, real processes and a real tmux socket.

The instrument taxonomy — and the "one sensor cannot corroborate itself" rule
that the destruction gate now turns on — gets its own suite in
``test__verdict_instruments.py``.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    INSTRUMENT_AGENT_SELF,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_LISTEN_BROKER,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_PID_NAMESPACE,
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
        Signal(
            SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber", INSTRUMENT_LISTEN_BROKER
        ),
        Signal(SOURCE_PROCESS, DEAD, "no apptainer pid", INSTRUMENT_PID_NAMESPACE),
        Signal(
            SOURCE_REGISTRY, DEAD, "recorded pid is reaped", INSTRUMENT_PID_NAMESPACE
        ),
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is stale", INSTRUMENT_AGENT_SELF),
    ]


@pytest.fixture
def all_unknown_signals():
    """Four "I could not look"s. The ``grant`` shape, as measured 2026-07-14."""
    return [
        Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            "could not ask the broker",
            INSTRUMENT_LISTEN_BROKER,
        ),
        Signal(SOURCE_PROCESS, UNKNOWN, "tmux probe FAILED", INSTRUMENT_HOST_TMUX),
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is 5086s stale", INSTRUMENT_HOST_TMUX),
        Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            "active row records pid=0",
            INSTRUMENT_NO_OBSERVATION,
        ),
    ]


@pytest.fixture
def corroborated_dead_signals():
    """Two GENUINELY INDEPENDENT INSTRUMENTS that each positively observed absence.

    tmux's own session bookkeeping says there is no session, AND the kernel says
    the recorded pid is reaped. Two different bookkeepers, two different failure
    modes — no single sensor malfunction can produce both. THIS is corroboration.
    """
    return [
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe succeeded; no session",
            INSTRUMENT_HOST_TMUX,
        ),
        Signal(
            SOURCE_REGISTRY,
            DEAD,
            "recorded pid=1234 is REAPED",
            INSTRUMENT_PID_NAMESPACE,
        ),
        Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            "0 subscribers — adapter detached",
            INSTRUMENT_LISTEN_BROKER,
        ),
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
        Signal(SOURCE_PROCESS, bogus, "not a ternary verdict", INSTRUMENT_HOST_TMUX)


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
    signals = [
        Signal(
            SOURCE_DELIVERY,
            ALIVE,
            "1 live inbox subscriber(s)",
            INSTRUMENT_LISTEN_BROKER,
        )
    ]
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
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe succeeded; no session",
            INSTRUMENT_HOST_TMUX,
        ),
        Signal(SOURCE_DELIVERY, UNKNOWN, "no listen to ask", INSTRUMENT_LISTEN_BROKER),
    ]
    # Act
    verdict = decide("scitex-dev", signals)
    # Assert
    assert verdict.verdict == DEAD


def test_a_single_dead_witness_does_not_authorise_destruction():
    """DEAD is a report. On one witness it is not a licence to destroy."""
    # Arrange
    signals = [
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe succeeded; no session",
            INSTRUMENT_HOST_TMUX,
        ),
        Signal(SOURCE_DELIVERY, UNKNOWN, "no listen to ask", INSTRUMENT_LISTEN_BROKER),
    ]
    # Act
    verdict = decide("scitex-dev", signals)
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# Rule 3: only a CORROBORATED dead may authorise a destructive remedy —
# and corroboration is counted in INSTRUMENTS, not in reports.
# --------------------------------------------------------------------------


def test_two_independent_dead_instruments_authorise_destruction(
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
    """Corroboration means INDEPENDENT sensors, not one signal counted twice."""
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "no session", INSTRUMENT_HOST_TMUX),
        Signal(SOURCE_PROCESS, DEAD, "no pane pid either", INSTRUMENT_HOST_TMUX),
    ]
    # Act
    verdict = decide("x", signals)
    # Assert
    assert verdict.may_destroy is False


def test_one_dissenting_alive_vetoes_destruction_outright():
    """The asymmetry is the point: a false DEAD destroys, a false ALIVE reports."""
    # Arrange
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "no session", INSTRUMENT_HOST_TMUX),
        Signal(SOURCE_REGISTRY, DEAD, "pid reaped", INSTRUMENT_PID_NAMESPACE),
        Signal(
            SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber", INSTRUMENT_LISTEN_BROKER
        ),
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
        Signal(SOURCE_HEARTBEAT, UNKNOWN, "beat is 5086s stale", INSTRUMENT_HOST_TMUX),
        Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            "active row records pid=0",
            INSTRUMENT_NO_OBSERVATION,
        ),
    ]
    # Act
    rendered = decide("grant", signals).render()
    # Assert
    assert "beat is 5086s stale" in rendered


def test_render_surfaces_the_unfalsifiable_pid_zero_as_evidence():
    # Arrange
    signals = [
        Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            "active row records pid=0",
            INSTRUMENT_NO_OBSERVATION,
        )
    ]
    # Act
    rendered = decide("grant", signals).render()
    # Assert
    assert "pid=0" in rendered


def test_render_shows_dissenting_signals_rather_than_hiding_them():
    # Arrange
    signals = [
        Signal(
            SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber", INSTRUMENT_LISTEN_BROKER
        ),
        Signal(SOURCE_PROCESS, DEAD, "no session", INSTRUMENT_HOST_TMUX),
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


def test_to_dict_evidence_names_the_instrument_behind_each_signal():
    """A ``--json`` consumer must be able to see that two DEADs were one sensor."""
    # Arrange
    signals = [Signal(SOURCE_REGISTRY, DEAD, "pid reaped", INSTRUMENT_PID_NAMESPACE)]
    # Act
    payload = decide("x", signals).to_dict()
    # Assert
    assert payload["evidence"][0]["instrument"] == INSTRUMENT_PID_NAMESPACE


# --------------------------------------------------------------------------
# Rule 1, the TIME dimension: a STALE heartbeat artefact does not overrule a
# LIVE probe of the SAME instrument (the reboot-recovery regression, 2026-07-15).
# --------------------------------------------------------------------------


def test_a_stale_heartbeat_alive_does_not_override_a_live_tmux_dead():
    """After a reboot the beat is still <600s but the tmux session is GONE.

    heartbeat and process are ONE instrument (host_tmux) for a TUI agent — the
    beat merely re-reports the snapshot the live probe reads. The live "no
    session" is newer than the file, so the fold must read DEAD, not ALIVE.
    (The bug: sac-start read ALIVE and refused to relaunch a dead agent.)
    """
    # Arrange — a fresh-looking beat, and a live tmux probe that found nothing.
    signals = [
        Signal(
            SOURCE_HEARTBEAT,
            ALIVE,
            "beaten 516s ago (< 600s) — sac listen observed this session",
            INSTRUMENT_HOST_TMUX,
        ),
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe SUCCEEDED and the server has NO session — positive absence",
            INSTRUMENT_HOST_TMUX,
        ),
    ]
    # Act
    verdict = decide("alpha", signals)
    # Assert
    assert verdict.verdict == DEAD


def test_the_stale_heartbeat_flip_still_authorises_no_destruction():
    """DEAD here rests on ONE instrument (host_tmux): it may report, not destroy.

    So the flip unblocks a NON-destructive sac-start, and cannot arm an auto-kill.
    """
    # Arrange
    signals = [
        Signal(
            SOURCE_HEARTBEAT, ALIVE, "beaten 516s ago (< 600s)", INSTRUMENT_HOST_TMUX
        ),
        Signal(SOURCE_PROCESS, DEAD, "no session", INSTRUMENT_HOST_TMUX),
    ]
    # Act
    verdict = decide("alpha", signals)
    # Assert
    assert verdict.may_destroy is False


def test_a_delivery_alive_still_overrides_a_live_tmux_dead():
    """The suppression is NARROW: only a heartbeat echo of the SAME instrument.

    A delivery ALIVE is a LIVE, independent observation (the broker saw the
    inbox), so it still vetoes — a working agent with a detached tmux is alive.
    """
    # Arrange
    signals = [
        Signal(
            SOURCE_DELIVERY,
            ALIVE,
            "1 live inbox subscriber",
            INSTRUMENT_LISTEN_BROKER,
        ),
        Signal(SOURCE_PROCESS, DEAD, "no session", INSTRUMENT_HOST_TMUX),
    ]
    # Act
    verdict = decide("alpha", signals)
    # Assert
    assert verdict.verdict == ALIVE


def test_a_self_heartbeat_is_not_suppressed_by_a_pid_dead():
    """A NON-tui beat is INSTRUMENT_AGENT_SELF — a real self-report, not a tmux
    echo — and the pid check is a DIFFERENT instrument, so it is not suppressed.
    """
    # Arrange
    signals = [
        Signal(
            SOURCE_HEARTBEAT,
            ALIVE,
            "the agent's own loop beat 5s ago",
            INSTRUMENT_AGENT_SELF,
        ),
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "recorded pid is reaped",
            INSTRUMENT_PID_NAMESPACE,
        ),
    ]
    # Act
    verdict = decide("alpha", signals)
    # Assert — a live self-report is real evidence of life; it wins.
    assert verdict.verdict == ALIVE


# --------------------------------------------------------------------------
# render() is TERSE: the claim, not the lecture; dissenters as tags.
# --------------------------------------------------------------------------


@pytest.fixture
def verbose_dead_signals():
    """A DEAD verdict's one signal: a claim followed by a long lecture tail."""
    return [
        Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux has no session for this agent — positive evidence of absence, "
            "from tmux's own bookkeeping",
            INSTRUMENT_HOST_TMUX,
        )
    ]


def test_render_shows_the_first_clause_of_a_verbose_detail(verbose_dead_signals):
    # Arrange — verbose_dead_signals fixture
    # Act
    rendered = decide("alpha", verbose_dead_signals).render()
    # Assert — the claim (before the em-dash) is shown.
    assert "tmux has no session for this agent" in rendered


def test_render_drops_the_lecture_after_the_em_dash(verbose_dead_signals):
    # Arrange — verbose_dead_signals fixture
    # Act
    rendered = decide("alpha", verbose_dead_signals).render()
    # Assert — the educational tail belongs in --json, not the one-liner.
    assert "positive evidence of absence" not in rendered


@pytest.fixture
def dissenting_heartbeat_signals():
    """A DEAD verdict where a suppressed same-instrument heartbeat ALIVE dissents."""
    return [
        Signal(SOURCE_PROCESS, DEAD, "tmux has no session", INSTRUMENT_HOST_TMUX),
        Signal(
            SOURCE_HEARTBEAT,
            ALIVE,
            "beaten 516s ago (< 600s) — sac listen observed this session recently",
            INSTRUMENT_HOST_TMUX,
        ),
    ]


def test_render_shows_a_dissenter_as_a_source_verdict_tag(dissenting_heartbeat_signals):
    # Arrange — dissenting_heartbeat_signals fixture
    # Act
    rendered = decide("alpha", dissenting_heartbeat_signals).render()
    # Assert — the dissenter is a compact tag.
    assert "heartbeat[alive]" in rendered


def test_render_omits_a_dissenters_verbose_prose(dissenting_heartbeat_signals):
    # Arrange — dissenting_heartbeat_signals fixture
    # Act
    rendered = decide("alpha", dissenting_heartbeat_signals).render()
    # Assert — the operator's "splurge" (the dissenter's prose) is gone.
    assert "sac listen observed this session" not in rendered
