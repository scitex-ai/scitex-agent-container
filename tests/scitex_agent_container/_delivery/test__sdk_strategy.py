"""The SDK strategy — and the statuses that must NOT be read as a delivery.

``dispatched`` is the dangerous one: it means "the agent looks reachable, here is
a command you can run to actually send it". Reading that as a delivered message
would rebuild the original bug one layer up, so it is pinned here explicitly.
"""

from __future__ import annotations

from scitex_agent_container._delivery._sdk_strategy import deliver_via_sdk
from scitex_agent_container._delivery._state import DeliveryState


class Sender:
    """A real ``sdk_send_fn(agent, payload) -> (bool | None, str)``."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def __call__(self, agent, payload):
        self.calls.append((agent, payload))
        return self._outcome


def test_default_sdk_send_preserves_pending_exchange_as_unknown_not_failure():
    # Arrange — exact non-final receipt shape returned by Hermes in production.
    import scitex_agent_container.cli_pkg._send as send_mod
    from scitex_agent_container._delivery._sdk_strategy import default_sdk_send

    exchange_id = "xch_20260914T071511Z_scitex-compute-03_208629"
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
    saved = send_mod.send_to_agent
    send_mod.send_to_agent = lambda *args, **kwargs: receipt
    try:
        # Act
        ok, detail = default_sdk_send("peer", "hi")
    finally:
        send_mod.send_to_agent = saved
    # Assert
    assert (
        ok,
        exchange_id in detail,
        f"/v1/exchanges/{exchange_id}" in detail,
        "accepted" in detail,
        "pending" in detail,
        "failed" not in detail,
    ) == (None, True, True, True, True, True)


def _sent(outcome):
    return deliver_via_sdk(
        DeliveryState(agent="peer"), "peer", "[sac-deliver:abc] hi", Sender(outcome)
    )


def test_completed_turn_reports_delivered_true():
    # Arrange
    outcome = (True, "send_to_agent completed the turn (status='ok')")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.is_payload_delivered is True


def test_completed_turn_reports_submitted_true():
    # Arrange
    outcome = (True, "send_to_agent completed the turn (status='ok')")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.is_payload_submitted is True


def test_refused_turn_reports_delivered_false():
    # Arrange
    outcome = (False, "send_to_agent refused the turn (status='error')")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.is_payload_delivered is False


def test_unknown_outcome_stays_unknown():
    # Arrange
    outcome = (None, "send_to_agent timed out waiting for the reply")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.is_payload_delivered is None


def test_pending_sdk_outcome_never_claims_completed_submission():
    # Arrange
    exchange_id = "xch_20260914T071511Z_scitex-compute-03_208629"
    outcome = (None, f"accepted exchange {exchange_id}, which remains pending")
    # Act
    state = _sent(outcome)
    reason = state.reason_for("is_payload_submitted")
    # Assert
    assert (
        state.is_payload_submitted,
        exchange_id in reason,
        "does not prove submission" in reason,
        "completed turn proves submission" in reason,
    ) == (None, True, True, False)


def test_sdk_path_leaves_pane_readable_none():
    # Arrange
    outcome = (True, "ok")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.is_pane_readable is None


def test_sdk_path_records_the_send_detail():
    # Arrange
    outcome = (True, "send_to_agent completed the turn (status='ok')")
    # Act
    state = _sent(outcome)
    # Assert
    assert state.raw["send_detail"] == "send_to_agent completed the turn (status='ok')"


def test_submission_reason_explains_no_composer():
    # Arrange
    outcome = (True, "ok")
    # Act
    state = _sent(outcome)
    # Assert
    assert "no composer" in state.reason_for("is_payload_submitted")


def test_sender_receives_the_tokenised_payload():
    # Arrange
    sender = Sender((True, "ok"))
    # Act
    deliver_via_sdk(DeliveryState(agent="peer"), "peer", "[sac-deliver:abc] hi", sender)
    # Assert
    assert sender.calls == [("peer", "[sac-deliver:abc] hi")]
