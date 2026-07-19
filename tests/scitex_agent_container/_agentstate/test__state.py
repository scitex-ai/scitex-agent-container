"""The dataclass contract: one fixed shape, tri-state fields, raw kept whole.

Two properties are load-bearing and both are asserted here rather than assumed:
the SHAPE never varies (so absence has somewhere to be reported), and a signal is
only ever True, False or None (so nothing can smuggle a truthy value past the
fold and land on a pole).
"""

from __future__ import annotations

from functools import partial

import pytest

from scitex_agent_container._agentstate import SIGNAL_NAMES, AgentState


def test_a_fresh_state_has_every_signal_none():
    """Nothing observed means nothing claimed — the default cannot assert health."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    signals = state.signals()
    # Assert
    assert set(signals.values()) == {None}


def test_signals_always_returns_the_full_declared_set():
    """A mapping that omits its unknowns is the list[str] bug in dict costume."""
    # Arrange
    state = AgentState(agent="alpha", is_tmux_live=True)
    # Act
    signals = state.signals()
    # Assert
    assert tuple(signals) == SIGNAL_NAMES


def test_a_partially_observed_state_still_renders_every_signal():
    # Arrange
    state = AgentState(agent="alpha", is_tmux_live=True)
    # Act
    signals = state.signals()
    # Assert
    assert signals["is_process_alive"] is None


def test_a_non_bool_signal_value_is_refused():
    """A truthy string would evaluate as a pole and never be noticed."""
    # Arrange
    building = partial(AgentState, agent="alpha", is_tmux_live="yes")
    # Act
    constructing = building
    # Assert
    with pytest.raises(TypeError, match="must be True, False or None"):
        constructing()


def test_an_unknown_reason_key_is_refused():
    """A reason filed under a typo'd signal is a reason nobody will ever read."""
    # Arrange
    building = partial(
        AgentState, agent="alpha", reasons={"is_definitely_fine": "sure"}
    )
    # Act
    constructing = building
    # Assert
    with pytest.raises(KeyError, match="unknown signal"):
        constructing()


def test_with_signal_records_the_value():
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    updated = state.with_signal("is_tmux_live", True, "the server lists it")
    # Assert
    assert updated.is_tmux_live is True


def test_with_signal_records_the_reason_beside_the_value():
    """A None that will not say WHY is a shrug wearing a type."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    updated = state.with_signal("is_tmux_live", None, "tmux socket unreachable")
    # Assert
    assert updated.reason_for("is_tmux_live") == "tmux socket unreachable"


def test_with_signal_keeps_the_raw_evidence_it_was_read_from():
    """A signal and the bytes it was read from must never be stored apart."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    updated = state.with_signal(
        "is_login_required", True, "frozen banner", pane_run1="LOGIN EXPIRED"
    )
    # Assert
    assert updated.raw["pane_run1"] == "LOGIN EXPIRED"


def test_with_signal_leaves_the_original_untouched():
    """Frozen means frozen — a builder returns a copy, never mutates in place."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    state.with_signal("is_tmux_live", True)
    # Assert
    assert state.is_tmux_live is None


def test_unknown_marks_every_signal_with_the_same_reason():
    """The missing-agent constructor: all None, and all of them say why."""
    # Arrange
    state = AgentState.unknown("scitex-hub", "no session on this host")
    # Act
    reasons = {name: state.reason_for(name) for name in SIGNAL_NAMES}
    # Assert
    assert set(reasons.values()) == {"no session on this host"}


def test_to_dict_carries_the_raw_signal_values():
    """「信号はそのまま書く」 — the JSON is the raw signals, not a rendering."""
    # Arrange
    state = AgentState(agent="alpha", is_tmux_live=True, is_process_alive=None)
    # Act
    payload = state.to_dict()
    # Assert
    assert payload["signals"]["is_process_alive"]["value"] is None


def test_to_dict_carries_the_spec_metadata_for_each_signal():
    """A consumer must be able to recompute our fold, which needs the spec."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    payload = state.to_dict()
    # Assert
    assert payload["signals"]["is_process_alive"]["decisive"] is True


def test_to_dict_carries_the_raw_captures():
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": "hello"})
    # Act
    payload = state.to_dict()
    # Assert
    assert payload["raw"]["pane_run1"] == "hello"


def test_observed_at_is_not_defaulted_to_now():
    """Dating an old reading to the moment it was rendered would falsify it."""
    # Arrange
    state = AgentState(agent="alpha")
    # Act
    stamp = state.observed_at
    # Assert
    assert stamp is None


# EOF
