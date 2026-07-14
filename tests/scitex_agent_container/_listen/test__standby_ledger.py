"""Tests for ``_listen/_standby_ledger.py`` — a failed check is never erased.

The rule under test: **a failure is a fact; one later success does not
un-happen it.** The counter this ledger replaces did ``consecutive_
unhealthy = 0`` on ANY single lucky reply, so a FLAPPING holder — which
is exactly what the live 7878 daemon does (HTTP 200 one minute,
``Connection refused`` the next) — never accumulated the consecutive
failures the take-over threshold demanded, and ``sac listen`` stood by
behind a dead holder forever.

The counter-invariant matters just as much: a genuinely healthy holder
that blips ONCE must not be destroyed. It clears its suspicion by
answering again, repeatedly.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

from scitex_agent_container._listen._holder_health import HolderHealth, HolderProbe
from scitex_agent_container._listen._standby_ledger import (
    HolderLedger,
    ListenTakeoverFailed,
    takeover_failure_message,
)

SERVING = HolderProbe(health=HolderHealth.SERVING, status=200)
UNREACHABLE = HolderProbe(health=HolderHealth.UNREACHABLE, status=-1)
NOT_SERVING = HolderProbe(health=HolderHealth.NOT_SERVING, status=503)


def _feed(ledger: HolderLedger, probes: list[HolderProbe]) -> None:
    for probe in probes:
        ledger.record(probe)


# ---------------------------------------------------------------------------
# The defect: a lucky reply must not erase a failure
# ---------------------------------------------------------------------------


def test_lucky_reply_does_not_erase_failure() -> None:
    # Arrange — THE regression. Old behaviour: the 200 reset the counter
    # to zero and the holder was announced "healthy" again.
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [UNREACHABLE, SERVING])
    # Assert — the failure still stands on the books.
    assert ledger.failures == 1


def test_flapping_holder_eventually_corroborates() -> None:
    # Arrange — miss, answer, miss: the operator's actual holder. With the
    # old counter this could never reach the threshold.
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [UNREACHABLE, SERVING, UNREACHABLE])
    # Assert — the wedged verdict is now corroborated and can be acted on.
    assert ledger.corroborated


def test_flapping_holder_stays_suspect_after_reply() -> None:
    # Arrange — while a failure stands un-cleared the holder is SUSPECT,
    # so the loop must not print "standing by behind healthy holder".
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [UNREACHABLE, SERVING])
    # Assert
    assert ledger.suspect


# ---------------------------------------------------------------------------
# The counter-invariant: a single blip must not destroy a healthy holder
# ---------------------------------------------------------------------------


def test_sustained_recovery_clears_the_suspicion() -> None:
    # Arrange — one blip, then the holder answers consistently. It is
    # healthy; killing it would BE the outage.
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [UNREACHABLE, SERVING, SERVING])
    # Assert
    assert ledger.failures == 0


def test_recovery_transition_is_reported() -> None:
    # Arrange — "the thing I said was broken now looks fine" is a
    # transition the operator must never have hidden from them, so the
    # ledger reports it for a LOUD log line.
    ledger = HolderLedger(threshold=2)
    _feed(ledger, [UNREACHABLE, SERVING])
    # Act
    recovered = ledger.record(SERVING)
    # Assert
    assert recovered is True


def test_recovered_holder_is_no_longer_suspect() -> None:
    # Arrange
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [UNREACHABLE, SERVING, SERVING])
    # Assert
    assert not ledger.suspect


def test_clean_holder_is_never_suspect() -> None:
    # Arrange — a holder that has never missed must stand by silently.
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [SERVING, SERVING, SERVING])
    # Assert
    assert not ledger.suspect


def test_clean_holder_reports_no_recovery() -> None:
    # Arrange — nothing to recover FROM, so no loud line is emitted.
    ledger = HolderLedger(threshold=2)
    # Act
    recovered = ledger.record(SERVING)
    # Assert
    assert recovered is False


# ---------------------------------------------------------------------------
# Both failure kinds count
# ---------------------------------------------------------------------------


def test_server_error_counts_as_failure() -> None:
    # Arrange — a 503 is an ANSWER but not health. It must accrue.
    ledger = HolderLedger(threshold=2)
    # Act
    _feed(ledger, [NOT_SERVING, NOT_SERVING])
    # Assert
    assert ledger.corroborated


def test_single_miss_is_not_corroborated() -> None:
    # Arrange — corroboration protects a daemon that merely has not
    # finished binding yet. One miss is never enough to act.
    ledger = HolderLedger(threshold=2)
    # Act
    ledger.record(UNREACHABLE)
    # Assert
    assert not ledger.corroborated


def test_reset_forgets_the_history() -> None:
    # Arrange — after a take-over the world changed; the old observations
    # no longer describe the process now holding the flock.
    ledger = HolderLedger(threshold=2)
    _feed(ledger, [UNREACHABLE, UNREACHABLE])
    # Act
    ledger.reset()
    # Assert
    assert ledger.failures == 0


# ---------------------------------------------------------------------------
# takeover_failure_message — loud + actionable
# ---------------------------------------------------------------------------


def test_failure_message_names_the_holder_pid() -> None:
    # Arrange
    # Act
    message = takeover_failure_message(
        host="127.0.0.1",
        port=7878,
        holder_pid=738982,
        probe=UNREACHABLE,
        failures=2,
        attempts=3,
        error="ignored SIGTERM",
    )
    # Assert
    assert "738982" in message


def test_failure_message_names_the_remedy() -> None:
    # Arrange
    # Act
    message = takeover_failure_message(
        host="127.0.0.1",
        port=7878,
        holder_pid=738982,
        probe=UNREACHABLE,
        failures=2,
        attempts=3,
        error="ignored SIGTERM",
    )
    # Assert
    assert "sac listen restart --force" in message


def test_failure_message_explains_the_refusal() -> None:
    # Arrange — the operator must understand WHY we did not force it, or
    # the next person will just add the SIGKILL back.
    # Act
    message = takeover_failure_message(
        host="127.0.0.1",
        port=7878,
        holder_pid=738982,
        probe=UNREACHABLE,
        failures=2,
        attempts=3,
        error="ignored SIGTERM",
    )
    # Assert
    assert "Refusing to SIGKILL" in message


def test_failure_message_states_the_evidence() -> None:
    # Arrange — what was OBSERVED, not merely what was concluded.
    # Act
    message = takeover_failure_message(
        host="127.0.0.1",
        port=7878,
        holder_pid=738982,
        probe=UNREACHABLE,
        failures=2,
        attempts=3,
        error="ignored SIGTERM",
    )
    # Assert
    assert "/v1/health" in message


def test_takeover_failed_is_a_runtime_error() -> None:
    # Arrange
    failure_type = ListenTakeoverFailed
    # Act
    is_runtime = issubclass(failure_type, RuntimeError)
    # Assert
    assert is_runtime
