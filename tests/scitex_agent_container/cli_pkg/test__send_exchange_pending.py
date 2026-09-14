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


def test_pending_exchange_preserves_exact_receipt_and_poll_surface() -> None:
    # Arrange — exact non-final receipt shape returned by Hermes in production.
    exchange_id = "xch_20260914T071511Z_scitex-compute-03_208629"
    hint = f"curl -sS http://127.0.0.1:19000/v1/exchanges/{exchange_id}"
    receipt = {
        "exchange_id": exchange_id,
        "receipt": {
            "state": "pending",
            "final": False,
            "delivery_mode": "steer",
        },
        "status_code": {
            "kind": "http",
            "code": 202,
            "message": f"accepted; poll `/v1/exchanges/{exchange_id}`",
        },
    }
    pending = PeerTimeoutPending(
        "accepted exchange remains pending",
        status="exchange_pending",
        timeout_s=2,
        raw_body=receipt,
        exchange_id=exchange_id,
        poll_hint=hint,
    )

    # Act
    payload = _pending_exchange_payload("scitex-hub", pending)

    # Assert
    assert (
        payload["exchange_id"],
        payload["receipt"],
        payload["poll_hint"],
        "error" in payload,
    ) == (exchange_id, receipt["receipt"], hint, False)
