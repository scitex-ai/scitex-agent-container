"""Tests for ``_lifecycle._verdict_refusal`` (PS-204 mirror).

NO MOCKS. The refusals are the REAL captured records under
``fixtures/refusals/`` (see ``test__verdict_refusal_read``'s docstring for their
provenance — they are the 2026-08-10 incidents verbatim, not authored for a
test). Collaborators are injected as plain callables and real
:class:`AgentConfig` objects; the fold is exercised through the production
:func:`decide`.

The load-bearing case here is the LAST one: on 2026-08-10 the operator's
messages REACHED ``scitex-cards`` — that is how they got refused — so a
delivery-ALIVE was available the whole time it was unable to act. If a
delivery-ALIVE still outranked the refusal, this change would report HEALTHY
for the exact incident it exists to catch.

STX-TQ002 AAA markers + STX-TQ007 one observable assert + STX-TQ003 names.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle._verdict import decide
from scitex_agent_container._lifecycle._verdict_instruments import (
    ALIVE,
    INSTRUMENT_LISTEN_BROKER,
    INSTRUMENT_TUI_SCREEN,
    INSTRUMENT_TURN_REFUSAL,
    SOURCE_DELIVERY,
    SOURCE_SCREEN,
    SOURCE_TRANSCRIPT,
    UNKNOWN,
    WEDGED,
    Signal,
)
from scitex_agent_container._lifecycle._verdict_refusal import refusal_signal
from scitex_agent_container._lifecycle._verdict_refusal_read import last_turn_refusal

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "refusals"
_QUOTA_FIXTURE = _FIXTURES / "quota_weekly_limit_20260810.jsonl"
_CLEAN_FIXTURE = _FIXTURES / "clean_turn_20260810.jsonl"


class _Config:
    """A minimal real config object — the signal only ever reads ``name``.

    A plain class rather than a mock: ``refusal_signal`` passes this straight to
    the injected ``find_fn``, so anything with the attribute is a faithful
    stand-in for the production ``AgentConfig`` here.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.runtime = "tui"


def _at(fixture: Path) -> float:
    """The real timestamp on a captured record, as epoch seconds."""
    return float(last_turn_refusal(fixture, now=0.0, stale_after_s=1e12).at or 0.0)


def _finds(path: Path):
    """A real ``find_fn`` that resolves to ``path``."""

    def find(_config):
        return path, ("<injected>",)

    return find


def _finds_nothing(_config):
    """A real ``find_fn`` for an agent whose transcript is not on this host."""
    return None, ("/promised/by/the/spec",)


# --- the signal ------------------------------------------------------------


def test_a_real_weekly_limit_transcript_yields_a_wedged_signal() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal(
        "scitex-cards",
        config=config,
        find_fn=_finds(_QUOTA_FIXTURE),
        now=_at(_QUOTA_FIXTURE) + 10.0,
    )
    # Assert
    assert signal.verdict == WEDGED


def test_the_wedged_signal_is_attributed_to_the_turn_refusal_instrument() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal(
        "scitex-cards",
        config=config,
        find_fn=_finds(_QUOTA_FIXTURE),
        now=_at(_QUOTA_FIXTURE) + 10.0,
    )
    # Assert
    assert signal.instrument == INSTRUMENT_TURN_REFUSAL


def test_the_wedged_signal_carries_the_remedy_for_a_quota_wall() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal(
        "scitex-cards",
        config=config,
        find_fn=_finds(_QUOTA_FIXTURE),
        now=_at(_QUOTA_FIXTURE) + 10.0,
    )
    # Assert
    assert "RESTART DOES NOT FIX THIS" in signal.detail


def test_a_real_ordinary_turn_yields_unknown_never_alive() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal(
        "scitex-cards",
        config=config,
        find_fn=_finds(_CLEAN_FIXTURE),
        now=_at(_CLEAN_FIXTURE) + 10.0,
    )
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_missing_config_is_unknown_not_a_verdict() -> None:
    # Arrange
    name = "scitex-cards"
    # Act
    signal = refusal_signal(name, config=None)
    # Assert
    assert signal.verdict == UNKNOWN


def test_an_unlocatable_transcript_is_unknown_not_healthy() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal("scitex-cards", config=config, find_fn=_finds_nothing)
    # Assert
    assert signal.verdict == UNKNOWN


def test_an_unlocatable_transcript_says_the_homes_were_only_a_promise() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal("scitex-cards", config=config, find_fn=_finds_nothing)
    # Assert
    assert "SPEC promises" in signal.detail


def test_an_unlocatable_transcript_names_the_homes_it_searched() -> None:
    # Arrange
    config = _Config("scitex-cards")
    # Act
    signal = refusal_signal("scitex-cards", config=config, find_fn=_finds_nothing)
    # Assert
    assert "/promised/by/the/spec" in signal.detail


# --- the fold: a refusal outranks a delivery-ALIVE -------------------------


def _delivery_alive() -> Signal:
    return Signal(
        SOURCE_DELIVERY,
        ALIVE,
        "the broker observed this agent's inbox adapter attached",
        INSTRUMENT_LISTEN_BROKER,
    )


def _refusal_wedged() -> Signal:
    return Signal(
        SOURCE_TRANSCRIPT,
        WEDGED,
        "the most recent assistant turn was REFUSED by the provider",
        INSTRUMENT_TURN_REFUSAL,
    )


def _screen_wedged() -> Signal:
    return Signal(
        SOURCE_SCREEN,
        WEDGED,
        "a frozen auth banner sits above the prompt",
        INSTRUMENT_TUI_SCREEN,
    )


def test_a_refusal_wedge_beats_a_delivery_alive() -> None:
    # Arrange — the 2026-08-10 shape: messages ARRIVED and were refused.
    signals = [_delivery_alive(), _refusal_wedged()]
    # Act
    verdict = decide("scitex-cards", signals)
    # Assert
    assert verdict.verdict == WEDGED


def test_a_refusal_wedge_means_the_agent_does_not_read_as_alive() -> None:
    # Arrange
    signals = [_delivery_alive(), _refusal_wedged()]
    # Act
    verdict = decide("scitex-cards", signals)
    # Assert
    assert verdict.is_alive is False


def test_a_refusal_wedge_never_authorises_destruction() -> None:
    # Arrange
    signals = [_delivery_alive(), _refusal_wedged()]
    # Act
    verdict = decide("scitex-cards", signals)
    # Assert
    assert verdict.may_destroy is False


def test_a_screen_wedge_still_loses_to_a_delivery_alive() -> None:
    # Arrange — the suppression is narrow: only the transcript instrument.
    signals = [_delivery_alive(), _screen_wedged()]
    # Act
    verdict = decide("grant", signals)
    # Assert
    assert verdict.verdict == ALIVE


def test_a_delivery_alive_still_wins_when_no_refusal_is_in_evidence() -> None:
    # Arrange
    signals = [_delivery_alive()]
    # Act
    verdict = decide("scitex-cards", signals)
    # Assert
    assert verdict.verdict == ALIVE
