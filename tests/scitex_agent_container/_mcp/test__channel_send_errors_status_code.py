"""``status_code`` (ADR-0007) on the ``a2a_send`` FAILURE payloads.

Companion to ``test__channel_tools_status_code.py`` (the SUCCESS side).
Pure unit tests on ``_error_payload`` — no I/O, no state.db, no
PostgreSQL, so these EXECUTE unconditionally.

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch`` — every
``SendError`` here is the real error-builder the production code calls.
AAA markers, one assertion per test (STX-TQ002 / STX-TQ007).
"""

from __future__ import annotations

from scitex_agent_container._mcp._channel_send_errors import (
    _error_payload,
    delivery_error,
    no_subscriber_error,
    not_running_error,
    unknown_target_error,
    unreachable_error,
)

# ---------------------------------------------------------------------------
# not_running_error -> scitex AGENT_UNAVAILABLE (the scitex-hpc shape)
# ---------------------------------------------------------------------------


def test_not_running_error_payload_carries_status_code():
    # Arrange
    exc = not_running_error("scitex-hpc")
    # Act
    payload = _error_payload(exc)
    # Assert
    assert "status_code" in payload


def test_not_running_error_status_code_is_scitex_agent_unavailable():
    # Arrange
    exc = not_running_error("scitex-hpc")
    # Act
    payload = _error_payload(exc)
    # Assert
    assert (payload["status_code"]["kind"], payload["status_code"]["code"]) == (
        "scitex",
        "AGENT_UNAVAILABLE",
    )


def test_not_running_error_status_code_is_final():
    # Arrange
    exc = not_running_error("scitex-hpc")
    # Act
    payload = _error_payload(exc)
    # Assert
    from scitex_dev.status import StatusCode

    sc = StatusCode.from_dict(payload["status_code"])
    assert sc.final is True


# ---------------------------------------------------------------------------
# unknown_target_error -> scitex NOT_RESOLVABLE
# ---------------------------------------------------------------------------


def test_unknown_target_error_status_code_is_scitex_not_resolvable():
    # Arrange
    exc = unknown_target_error("sac", ["scitex-agent-container"])
    # Act
    payload = _error_payload(exc)
    # Assert
    assert (payload["status_code"]["kind"], payload["status_code"]["code"]) == (
        "scitex",
        "NOT_RESOLVABLE",
    )


def test_unknown_target_error_status_code_is_final():
    # Arrange
    exc = unknown_target_error("sac", ["scitex-agent-container"])
    # Act
    payload = _error_payload(exc)
    # Assert
    from scitex_dev.status import StatusCode

    sc = StatusCode.from_dict(payload["status_code"])
    assert sc.final is True


# ---------------------------------------------------------------------------
# The deliberate scope limit — no status_code for the ambiguous / transport
# error classes (see the PR body: the runtime genuinely cannot tell these
# apart from a live-but-detached adapter without inventing a distinction).
# ---------------------------------------------------------------------------


def test_no_subscriber_error_carries_no_status_code():
    # Arrange
    exc = no_subscriber_error("bob")
    # Act
    payload = _error_payload(exc)
    # Assert
    assert "status_code" not in payload


def test_unreachable_error_carries_no_status_code():
    # Arrange
    exc = unreachable_error("bob", RuntimeError("connection refused"))
    # Act
    payload = _error_payload(exc)
    # Assert
    assert "status_code" not in payload


def test_delivery_error_carries_no_status_code():
    # Arrange
    exc = delivery_error("bob", 502, "bad gateway")
    # Act
    payload = _error_payload(exc)
    # Assert
    assert "status_code" not in payload


# EOF
