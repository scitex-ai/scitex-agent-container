"""Tests for ``scitex_agent_container.cli_pkg._send_status_code``.

Pure unit tests for the ``scitex_dev.status.StatusCode`` construction
helpers (ADR-0007) — no I/O, no state.db, no PostgreSQL, so these EXECUTE
on every host regardless of whether a writable fleet database is
reachable (contrast with ``test__send.py``'s ``pg_schema``-gated suite,
which legitimately skips on a read-only replica host).

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch`` — nothing
here needs a seam; every call constructs a real ``StatusCode``.
STX-TQ007: each test asserts exactly one fact. STX-TQ002: Arrange / Act /
Assert markers on every test.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._send_status_code import (
    agent_unavailable_status_code,
    completed_status_code,
    dispatch_accepted_status_code,
    not_resolvable_status_code,
    timed_out_status_code,
)

# ---------------------------------------------------------------------------
# dispatch_accepted_status_code — the non-blocking agent_send dispatch shape
# ---------------------------------------------------------------------------


def test_dispatch_accepted_is_http_kind():
    # Arrange
    name = "alpha"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=True)
    # Assert
    assert sc.kind == "http"


def test_dispatch_accepted_is_code_202():
    # Arrange
    name = "alpha"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=True)
    # Assert
    assert sc.code == 202


def test_dispatch_accepted_is_not_final():
    # Arrange
    name = "alpha"
    # Act — 202 is "received, still working": accepted, never confirmed read.
    sc = dispatch_accepted_status_code(name=name, verified=True)
    # Assert
    assert sc.final is False


def test_dispatch_accepted_is_ok_within_http():
    # Arrange
    name = "alpha"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=True)
    # Assert
    assert sc.ok is True


def test_dispatch_accepted_verified_message_says_local_probe_confirmed():
    # Arrange
    name = "alpha"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=True)
    # Assert
    assert "confirmed" in sc.message


def test_dispatch_accepted_unverified_message_says_not_verified():
    # Arrange — the scitex-hpc-shaped case: cross-host / brokered, no local
    # probe could run.
    name = "scitex-hpc"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=False)
    # Assert
    assert "NOT verified" in sc.message


def test_dispatch_accepted_names_a_probe_in_backticks():
    # Arrange — M2: a non-final http 202 MUST name a runnable probe.
    name = "alpha"
    # Act
    sc = dispatch_accepted_status_code(name=name, verified=False)
    # Assert
    assert "`sac agents status alpha`" in sc.message


# ---------------------------------------------------------------------------
# agent_unavailable_status_code — the scitex-hpc case, correctly labelled
# ---------------------------------------------------------------------------


def test_agent_unavailable_is_scitex_kind():
    # Arrange
    name, reason = "scitex-hpc", "no live session"
    # Act
    sc = agent_unavailable_status_code(name, reason)
    # Assert
    assert sc.kind == "scitex"


def test_agent_unavailable_is_code_agent_unavailable():
    # Arrange
    name, reason = "scitex-hpc", "no live session"
    # Act
    sc = agent_unavailable_status_code(name, reason)
    # Assert
    assert sc.code == "AGENT_UNAVAILABLE"


def test_agent_unavailable_is_final():
    # Arrange — a registered-but-not-running verdict is a completed fact.
    name, reason = "scitex-hpc", "no live session"
    # Act
    sc = agent_unavailable_status_code(name, reason)
    # Assert
    assert sc.final is True


def test_agent_unavailable_is_not_ok():
    # Arrange
    name, reason = "scitex-hpc", "no live session"
    # Act
    sc = agent_unavailable_status_code(name, reason)
    # Assert
    assert sc.ok is False


def test_agent_unavailable_message_carries_the_reason():
    # Arrange
    name = "scitex-hpc"
    reason = "no active instances row and no durable claim"
    # Act
    sc = agent_unavailable_status_code(name, reason)
    # Assert
    assert reason in sc.message


# ---------------------------------------------------------------------------
# not_resolvable_status_code
# ---------------------------------------------------------------------------


def test_not_resolvable_is_scitex_kind():
    # Arrange
    name, reason = "sac", "no agent by that name"
    # Act
    sc = not_resolvable_status_code(name, reason)
    # Assert
    assert sc.kind == "scitex"


def test_not_resolvable_is_code_not_resolvable():
    # Arrange
    name, reason = "sac", "no agent by that name"
    # Act
    sc = not_resolvable_status_code(name, reason)
    # Assert
    assert sc.code == "NOT_RESOLVABLE"


def test_not_resolvable_is_final():
    # Arrange
    name, reason = "sac", "no agent by that name"
    # Act
    sc = not_resolvable_status_code(name, reason)
    # Assert
    assert sc.final is True


# ---------------------------------------------------------------------------
# completed_status_code — a genuine blocking round trip
# ---------------------------------------------------------------------------


def test_completed_is_http_200():
    # Arrange
    name = "alpha"
    # Act
    sc = completed_status_code(name)
    # Assert
    assert (sc.kind, sc.code) == ("http", 200)


def test_completed_is_final():
    # Arrange
    name = "alpha"
    # Act
    sc = completed_status_code(name)
    # Assert
    assert sc.final is True


def test_completed_is_ok():
    # Arrange
    name = "alpha"
    # Act
    sc = completed_status_code(name)
    # Assert
    assert sc.ok is True


# ---------------------------------------------------------------------------
# timed_out_status_code — 504, "I stopped waiting", never a peer verdict
# ---------------------------------------------------------------------------


def test_timed_out_is_http_504():
    # Arrange
    name, timeout_seconds = "alpha", 60
    # Act
    sc = timed_out_status_code(name, timeout_seconds)
    # Assert
    assert (sc.kind, sc.code) == ("http", 504)


def test_timed_out_is_final():
    # Arrange — 504 is not in http's non_final list; the CALLER gave up,
    # which is itself a completed fact even though the peer's own outcome
    # remains unknown.
    name, timeout_seconds = "alpha", 60
    # Act
    sc = timed_out_status_code(name, timeout_seconds)
    # Assert
    assert sc.final is True


def test_timed_out_is_not_ok():
    # Arrange
    name, timeout_seconds = "alpha", 60
    # Act
    sc = timed_out_status_code(name, timeout_seconds)
    # Assert
    assert sc.ok is False


def test_timed_out_message_quotes_the_timeout_value():
    # Arrange
    name, timeout_seconds = "alpha", 60
    # Act
    sc = timed_out_status_code(name, timeout_seconds)
    # Assert
    assert "60s" in sc.message


# EOF
