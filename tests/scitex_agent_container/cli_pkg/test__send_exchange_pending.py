"""Pure contract tests for an accepted, non-final turn exchange."""

from scitex_dev.status import StatusCode

from scitex_agent_container._network.peer import PeerTimeoutPending
from scitex_agent_container.cli_pkg._send import _pending_exchange_payload


def test_pending_exchange_is_successful_nonfinal_and_never_a_resend_hint() -> None:
    # Arrange
    exchange_id = "xch_20260914T000000Z_compute-03_abcdef"
    hint = f"curl -sS http://127.0.0.1:19000/v1/exchanges/{exchange_id}"
    pending = PeerTimeoutPending(
        "accepted exchange remains pending",
        status="exchange_pending",
        timeout_s=2,
        exchange_id=exchange_id,
        poll_hint=hint,
    )

    # Act
    payload = _pending_exchange_payload("scitex-hub", pending)
    status = StatusCode.from_dict(payload["status_code"])

    # Assert
    assert (
        payload["status"],
        payload["exchange_id"],
        status.ok,
        status.final,
        "do not resend" in status.message,
        hint in status.message,
    ) == ("pending", exchange_id, True, False, True, True)
