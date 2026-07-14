"""Tests for ``_listen/_standby_signal.py`` — a prompt, clean stop mid-decision.

``resolve_startup`` may spend a few seconds re-checking a holder that failed
its health check. A ``systemctl stop`` (or Ctrl-C) landing in that window must
produce a prompt clean exit — not a ``KeyboardInterrupt`` traceback, and not a
process that ignores the signal.

No-mocks (PA-306 / STX-NM001-003): the handler the guard installed is invoked
exactly as the kernel would — the REAL registered callable — and handler
restoration is asserted against the REAL ``signal.getsignal``.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import signal

from scitex_agent_container._listen._standby_signal import StopFlag, stop_flag_guard


# ---------------------------------------------------------------------------
# StopFlag — a one-way latch
# ---------------------------------------------------------------------------


def test_fresh_flag_is_not_set() -> None:
    # Arrange
    flag = StopFlag()
    # Act
    tripped = flag.is_set()
    # Assert
    assert tripped is False


def test_tripped_flag_reports_set() -> None:
    # Arrange
    flag = StopFlag()
    # Act
    flag.trip(signal.SIGTERM, None)
    # Assert
    assert flag.is_set() is True


def test_flag_stays_set_once_tripped() -> None:
    # Arrange — a one-way latch: the loop polls it, so a stop must not be
    # able to "un-happen" between polls.
    flag = StopFlag()
    flag.trip()
    # Act
    still_set = flag.is_set()
    # Assert
    assert still_set is True


# ---------------------------------------------------------------------------
# stop_flag_guard — real signal wiring + handler restoration
# ---------------------------------------------------------------------------


def test_guard_handler_trips_the_flag() -> None:
    # Arrange / Act — invoke the REAL handler the guard registered, exactly
    # as the kernel would.
    with stop_flag_guard() as flag:
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        # Act
        tripped = flag.is_set()
    # Assert
    assert tripped is True


def test_guard_restores_prior_sigterm_handler() -> None:
    # Arrange
    prior = signal.getsignal(signal.SIGTERM)
    # Act
    with stop_flag_guard():
        pass
    # Assert
    assert signal.getsignal(signal.SIGTERM) is prior


def test_guard_restores_prior_sigint_handler() -> None:
    # Arrange — SIGINT too: an interactive Ctrl-C must not leave the process
    # with the guard's handler after the decision is made.
    prior = signal.getsignal(signal.SIGINT)
    # Act
    with stop_flag_guard():
        pass
    # Assert
    assert signal.getsignal(signal.SIGINT) is prior


def test_guard_installs_its_own_sigterm_handler() -> None:
    # Arrange — inside the guard the handler must be the flag's trip method,
    # not the default disposition (which would kill the process outright).
    # Act
    with stop_flag_guard() as flag:
        installed = signal.getsignal(signal.SIGTERM)
    # Assert
    assert installed == flag.trip


def test_untripped_flag_survives_the_guard() -> None:
    # Arrange — no signal arrives; the loop must keep running.
    # Act
    with stop_flag_guard() as flag:
        pass
    # Assert
    assert flag.is_set() is False
