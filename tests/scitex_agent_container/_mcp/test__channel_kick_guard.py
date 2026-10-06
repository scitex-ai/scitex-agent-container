"""``KickLedger`` admits every first kick and collapses storms.

TQ: AAA markers, >=3-word names, one assertion each. No sleeps: time is
an injectable clock the tests drive by hand.
"""

from __future__ import annotations

from scitex_agent_container._mcp._channel_kick_guard import (
    KickLedger,
    KickPolicy,
)


class _Clock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        # Arrange
        self.now = 1000.0

    def __call__(self) -> float:
        # Act
        return self.now

    def advance(self, seconds: float) -> None:
        # Act
        self.now += seconds


def test_first_kick_for_new_message_is_admitted() -> None:
    # Arrange
    ledger = KickLedger(clock=_Clock())
    # Act
    decision = ledger.check("figrecipe", "m1")
    # Assert
    assert decision == "admit"


def test_same_message_id_within_ttl_is_duplicate() -> None:
    # Arrange
    ledger = KickLedger(clock=_Clock())
    ledger.record("figrecipe", "m1")
    # Act
    decision = ledger.check("figrecipe", "m1")
    # Assert
    assert decision == "duplicate"


def test_same_message_id_after_ttl_is_admitted() -> None:
    # Arrange
    clock = _Clock()
    ledger = KickLedger(
        KickPolicy(dedupe_ttl_s=60.0), clock=clock
    )
    ledger.record("figrecipe", "m1")
    # Act
    clock.advance(61.0)
    decision = ledger.check("figrecipe", "m1")
    # Assert
    assert decision == "admit"


def test_eleventh_kick_inside_window_is_rate_limited() -> None:
    # Arrange
    clock = _Clock()
    ledger = KickLedger(
        KickPolicy(max_kicks_per_window=10, window_s=60.0), clock=clock
    )
    for i in range(10):
        ledger.record("figrecipe", f"m{i}")
    # Act
    decision = ledger.check("figrecipe", "m10")
    # Assert
    assert decision == "rate_limited"


def test_kicks_resume_after_window_slides_past() -> None:
    # Arrange
    clock = _Clock()
    ledger = KickLedger(
        KickPolicy(max_kicks_per_window=1, window_s=60.0), clock=clock
    )
    ledger.record("figrecipe", "m0")
    # Act
    clock.advance(61.0)
    decision = ledger.check("figrecipe", "m1")
    # Assert
    assert decision == "admit"


def test_rate_limit_is_per_recipient_not_global() -> None:
    # Arrange
    clock = _Clock()
    ledger = KickLedger(
        KickPolicy(max_kicks_per_window=1, window_s=60.0), clock=clock
    )
    ledger.record("figrecipe", "m0")
    # Act
    decision = ledger.check("scitex-ui", "m0")
    # Assert
    assert decision == "admit"


def test_missing_msg_id_cannot_dedupe_so_admits() -> None:
    # Arrange
    ledger = KickLedger(clock=_Clock())
    ledger.record("figrecipe", None)
    # Act
    decision = ledger.check("figrecipe", None)
    # Assert
    assert decision == "admit"
