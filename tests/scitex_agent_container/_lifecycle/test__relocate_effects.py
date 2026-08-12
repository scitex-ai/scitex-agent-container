"""The gate on the irreversible step: retirement happens only after BOTH confirmations.

The driver already stops at the first non-yes, so reaching ``finish`` implies the
two confirmations. Implication is not a check, and this is the step that moves a
human's conversation out from under them — so it is re-checked, and that re-check
is what these tests pin.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_effects import (
    RelocateAdapters,
    build_effects,
)
from scitex_agent_container._lifecycle._relocate_shell import Shell


def _adapters(*, arrival, handshake) -> RelocateAdapters:
    return RelocateAdapters(
        agent="a",
        spec={},
        from_host="src",
        to_host="tgt",
        source=Shell(host="src", is_local=True),
        target=Shell(host="tgt"),
        stamp="20260811T000000Z",
        source_dir="/src/projects/-p",
        arrival_confirmed=arrival,
        handshake_confirmed=handshake,
    )


def test_retirement_is_refused_when_arrival_was_never_confirmed() -> None:
    # Arrange: the src -> tgt leg. Moving the source's transcript aside on an
    # unconfirmed copy is how the only copy of a conversation goes missing.
    adapters = _adapters(arrival=None, handshake=True)
    # Act
    result = adapters.finish()
    # Assert
    assert result.ok is False


def test_retirement_is_refused_when_arrival_failed() -> None:
    # Arrange: the same leg, observed negative rather than unobserved.
    adapters = _adapters(arrival=False, handshake=True)
    # Act
    result = adapters.finish()
    # Assert
    assert result.ok is False


def test_retirement_is_refused_when_the_handshake_was_never_observed() -> None:
    # Arrange: the tgt -> src leg. UNKNOWN refuses exactly as firmly as a
    # failure — retiring on an unproven target is the 2026-08-07 shape with the
    # source's memory moved out of reach.
    adapters = _adapters(arrival=True, handshake=None)
    # Act
    result = adapters.finish()
    # Assert
    assert result.ok is False


def test_the_refusal_names_which_confirmation_was_missing() -> None:
    # Arrange: two different gates, two different next actions. A refusal that
    # says only "not confirmed" sends the reader to check both.
    adapters = _adapters(arrival=True, handshake=None)
    # Act
    result = adapters.finish()
    # Assert
    assert "handshake_confirmed" in result.detail


def test_nothing_is_written_when_the_gate_refuses() -> None:
    # Arrange: the refusal must come BEFORE the residency write, not after it —
    # a residency row for a relocation that then refused to finish would record
    # a move that did not happen.
    adapters = _adapters(arrival=True, handshake=None)
    # Act
    adapters.finish()
    # Assert
    assert adapters.log == []


def test_the_driver_is_given_an_effect_for_every_phase() -> None:
    # Arrange: the driver REFUSES to run with a phase missing, because an absent
    # effect would journal as done having changed nothing — the most convincing
    # possible imitation of a successful relocation.
    from scitex_agent_container._lifecycle._relocate_execute import _missing_effects

    effects = build_effects(_adapters(arrival=True, handshake=True))
    # Act
    missing = _missing_effects(effects)
    # Assert
    assert missing == ()
