"""The spec table's own invariants — especially "decisive requires DIRECT".

The decisive amendment is the one place this design lets a single signal
override every other signal's UNKNOWN. That power is safe only while it is
restricted to things we read FIRST-HAND, so the restriction is enforced by
:func:`validate_specs` rather than asked for in prose — and these tests prove the
enforcement actually fires, by handing it tables that violate each rule.
"""

from __future__ import annotations

from functools import partial

import pytest

from scitex_agent_container._agentstate import (
    DECISIVE_SIGNALS,
    LOAD_BEARING,
    OBSERVATION_DIRECT,
    OBSERVATION_INFERRED,
    SIGNAL_NAMES,
    SIGNALS,
    SignalSpec,
    spec_for,
    validate_specs,
)


def a_spec(**overrides) -> SignalSpec:
    """A minimal valid spec entry, overridable one field at a time."""
    fields = {
        "name": "is_tmux_live",
        "reads": "a thing",
        "healthy": True,
        "load_bearing": True,
        "why": "because",
        "decisive": False,
        "observation": OBSERVATION_DIRECT,
    }
    fields.update(overrides)
    return SignalSpec(**fields)


# ---------------------------------------------------------------------------
# The shipped table is valid and says what the design says it says.
# ---------------------------------------------------------------------------


def test_the_shipped_spec_table_validates():
    # Arrange
    specs = SIGNALS
    # Act
    validate_specs(specs)
    # Assert
    assert len(specs) == len(SIGNAL_NAMES)


def test_every_decisive_signal_is_directly_observed():
    """The invariant, asserted against the REAL table, not only the validator."""
    # Arrange
    decisive = [spec_for(name) for name in DECISIVE_SIGNALS]
    # Act
    observations = {spec.observation for spec in decisive}
    # Assert
    assert observations == {OBSERVATION_DIRECT}


def test_every_decisive_signal_is_load_bearing():
    # Arrange
    decisive = [spec_for(name) for name in DECISIVE_SIGNALS]
    # Act
    load_bearing = {spec.load_bearing for spec in decisive}
    # Assert
    assert load_bearing == {True}


def test_is_process_alive_is_the_decisive_signal():
    """Named explicitly: the amendment is about ONE signal, read from /proc."""
    # Arrange
    expected = ("is_process_alive",)
    # Act
    actual = DECISIVE_SIGNALS
    # Assert
    assert actual == expected


def test_the_login_expired_signals_are_all_load_bearing():
    """The four signals that serve the one problem in focus must carry weight."""
    # Arrange
    focus = {"is_tmux_live", "is_process_alive", "is_login_required"}
    # Act
    load_bearing = set(LOAD_BEARING)
    # Assert
    assert focus <= load_bearing


def test_signals_known_to_read_unhealthy_on_healthy_agents_are_evidence_only():
    """Promoting any of these would flag a working fleet — measured, not feared."""
    # Arrange
    false_red_prone = {
        "is_inbox_reachable",
        "is_heartbeat_fresh",
        "is_registry_active",
        "is_session_advancing",
        "is_at_idle_prompt",
    }
    # Act
    load_bearing = set(LOAD_BEARING)
    # Assert
    assert not (false_red_prone & load_bearing)


def test_every_signal_states_why_it_is_or_is_not_load_bearing():
    """A criterion whose rationale is unwritten is one nobody can revise safely."""
    # Arrange
    specs = SIGNALS
    # Act
    unexplained = [spec.name for spec in specs if not spec.why.strip()]
    # Assert
    assert unexplained == []


# ---------------------------------------------------------------------------
# The validator FIRES. Each rule is proven by a table that breaks it.
# ---------------------------------------------------------------------------


def test_a_decisive_inferred_signal_is_refused():
    """THE GUARD: a cached verdict may be evidence, never a short-circuit.

    This is how a stale row would get to overrule a live reading.
    """
    # Arrange
    bad = (a_spec(decisive=True, observation=OBSERVATION_INFERRED),)
    # Act
    validating = partial(validate_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="DECISIVE REQUIRES DIRECT OBSERVATION"):
        validating()


def test_a_decisive_non_load_bearing_signal_is_refused():
    # Arrange
    bad = (a_spec(decisive=True, load_bearing=False),)
    # Act
    validating = partial(validate_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="decisive but not load-bearing"):
        validating()


def test_a_duplicate_signal_name_is_refused():
    """One entry silently shadowing another is a criterion nobody knows is there."""
    # Arrange
    bad = (a_spec(), a_spec())
    # Act
    validating = partial(validate_specs, bad)
    # Assert
    with pytest.raises(ValueError, match="duplicate signal"):
        validating()


def test_a_valid_decisive_direct_signal_is_accepted():
    """The CONTROL: the validator must not simply reject everything decisive."""
    # Arrange
    good = (a_spec(decisive=True, observation=OBSERVATION_DIRECT),)
    # Act
    validate_specs(good)
    # Assert
    assert good[0].decisive is True


# ---------------------------------------------------------------------------
# Lookups refuse to invent.
# ---------------------------------------------------------------------------


def test_an_unknown_signal_name_raises_rather_than_defaulting():
    """A typo must fail loudly, not silently create an unspecced criterion."""
    # Arrange
    name = "is_definitely_fine"
    # Act
    looking_up = partial(spec_for, name)
    # Assert
    with pytest.raises(KeyError, match="unknown signal"):
        looking_up()


# EOF
