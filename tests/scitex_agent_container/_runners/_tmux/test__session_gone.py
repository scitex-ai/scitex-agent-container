"""wait_session_gone: poll until a killed session disappears, bounded."""

from __future__ import annotations

from scitex_agent_container._runners._tmux._session_gone import wait_session_gone


class _Clock:
    """Deterministic monotonic clock advanced only by the fake sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _exists_for(polls_alive: int):
    """An ``exists`` that reports alive for the first ``polls_alive`` calls."""
    calls = {"n": 0}

    def exists(_name: str) -> bool:
        calls["n"] += 1
        return calls["n"] <= polls_alive

    return exists


def test_already_gone_session_returns_true_without_sleeping() -> None:
    # Arrange
    clock = _Clock()
    # Act
    gone = wait_session_gone(
        "s", exists=_exists_for(0), sleep_fn=clock.sleep, clock=clock
    )
    # Assert
    assert (gone, clock.sleeps) == (True, [])


def test_session_that_lingers_past_old_fixed_sleep_is_reported_gone() -> None:
    # Arrange: still listed for 1.0 s (ten 0.1 s polls), longer than the old
    # fixed 0.5 s sleep that misreported a successful stop as failed.
    clock = _Clock()
    # Act
    gone = wait_session_gone(
        "s", exists=_exists_for(10), sleep_fn=clock.sleep, clock=clock
    )
    # Assert
    assert gone is True


def test_session_that_never_disappears_returns_false_at_deadline() -> None:
    # Arrange
    clock = _Clock()
    # Act
    gone = wait_session_gone(
        "s",
        exists=lambda _name: True,
        timeout_s=1.0,
        sleep_fn=clock.sleep,
        clock=clock,
    )
    # Assert
    assert gone is False


def test_deadline_bounds_total_wait() -> None:
    # Arrange
    clock = _Clock()
    # Act
    wait_session_gone(
        "s",
        exists=lambda _name: True,
        timeout_s=1.0,
        sleep_fn=clock.sleep,
        clock=clock,
    )
    # Assert
    assert clock.now <= 1.0 + 0.1 + 1e-9
