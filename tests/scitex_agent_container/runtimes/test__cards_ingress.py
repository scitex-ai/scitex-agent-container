from __future__ import annotations

import asyncio
import threading

import pytest

from scitex_agent_container.runtimes import _cards_ingress as ingress
from scitex_agent_container.runtimes import _tui_turn_bridge as turn_bridge


def test_durable_reconcile_bounds_doorbell_loss_to_two_seconds():
    # Arrange
    expected_reconcile_interval_s = 2.0

    # Act
    actual_reconcile_interval_s = ingress.DEFAULT_RECONCILE_INTERVAL_S

    # Assert
    assert actual_reconcile_interval_s == expected_reconcile_interval_s


def test_consumer_requires_canonical_shared_store():
    """A retired Cards-specific alias cannot select a private database."""
    # Arrange
    environ = {"SCITEX_CARDS_DB": "postgresql://wrong:55432/private"}
    # Act
    ctx = pytest.raises(RuntimeError, match="requires SCITEX_STORE_DSN")
    # Assert
    with ctx:
        ingress.canonical_store_dsn(environ)


def test_consumer_selects_only_canonical_store():
    # Arrange
    canonical = "postgresql://scitex-primary:55432/scitex"
    environ = {
        "SCITEX_STORE_DSN": canonical,
        "SCITEX_CARDS_DB": "postgresql://wrong:55432/private",
    }
    # Act
    observed = ingress.canonical_store_dsn(environ)
    # Assert
    assert observed == canonical


@pytest.mark.parametrize(
    "value, message",
    [
        ("/tmp/private.db", "must name the canonical shared PostgreSQL store"),
        (
            "postgresql://scitex-primary:5432/scitex",
            "must use the canonical shared PostgreSQL port 55432",
        ),
    ],
)
def test_canonical_store_rejects_private_or_wrong_port_targets(value, message):
    # Arrange
    environ = {"SCITEX_STORE_DSN": value}
    # Act
    ctx = pytest.raises(RuntimeError, match=message)
    # Assert
    with ctx:
        ingress.canonical_store_dsn(environ)


def test_dm_is_rendered_with_sender_message_and_durable_id():
    # Arrange
    record = {
        "id": "m_5e58744beb1c",
        "event_type": "dm",
        "actor": "operator",
        "body": "Please inspect the signup failure.",
        "card_id": "dm:operator::scitex-hub",
        "_persisted": True,
    }
    # Act
    event = ingress.event_from_notification(record)
    # Assert
    assert event == {
        "msg_id": "m_5e58744beb1c",
        "cards_notification_id": "m_5e58744beb1c",
        "kind": "message",
        "from_agent": "operator",
        "content": "Please inspect the signup failure.",
        "conversation_id": "dm:operator::scitex-hub",
        "card_id": "dm:operator::scitex-hub",
        "_persisted": True,
    }


def test_cards_exchange_id_is_forwarded_without_substitution():
    # Arrange
    record = {
        "id": "n_visible",
        "msg_id": "m_visible",
        "event_type": "dm",
        "actor": "operator",
        "body": "Please inspect signup.",
        "exchange_id": "xch_20260912T000000Z_cards_abcdef",
    }

    # Act
    event = ingress.event_from_notification(record)

    # Assert
    assert (
        event["msg_id"],
        event["cards_notification_id"],
        event["exchange_id"],
    ) == (
        "m_visible",
        "n_visible",
        "xch_20260912T000000Z_cards_abcdef",
    )


def test_cards_notification_is_acked_only_after_positive_visible_delivery():
    # Arrange
    calls = []

    def poll(agent, **kwargs):
        calls.append(("poll", agent, kwargs))
        return {
            "store": "postgresql://cards-primary",
            "unconfirmed": ["m_visible"],
            "notifications": [
                {
                    "id": "m_visible",
                    "event_type": "dm",
                    "actor": "operator",
                    "body": "hello hub",
                    "card_id": "dm:operator::scitex-hub",
                }
            ],
        }

    async def deliver(event, **kwargs):
        calls.append(("visible", event, kwargs))

    def ack(agent, ids, **kwargs):
        calls.append(("ack", agent, ids, kwargs))
        return {"confirmed": ids, "already_confirmed": [], "unknown": []}

    # Act
    delivered = asyncio.run(
        ingress.drain_once(
            name="scitex-hub",
            turn_url="http://127.0.0.1:19001/v1/turn",
            bearer="secret",
            poll_notifications=poll,
            ack_notifications=ack,
            deliver=deliver,
        )
    )
    # Assert
    assert (delivered, [call[0] for call in calls], calls[-1]) == (
        1,
        ["poll", "visible", "ack"],
        (
            "ack",
            "scitex-hub",
            ["m_visible"],
            {"store": "postgresql://cards-primary"},
        ),
    )


def test_confirmed_cards_notification_projects_bounded_reviewer_lease():
    # Arrange
    leases = []
    order = []

    def poll(_agent, **_kwargs):
        return {
            "store": "postgresql://cards-primary",
            "unconfirmed": ["n-lease"],
            "notifications": [
                {
                    "id": "n-lease",
                    "event_type": "review-assigned",
                    "actor": "scitex-cards",
                    "body": "Review card-1",
                    "card_id": "card-1",
                    "lease_role": "reviewer",
                    "lease_expires_at": 200.0,
                }
            ],
        }

    async def deliver(_event, **_kwargs):
        return None

    def ack(_agent, ids, **_kwargs):
        order.append("ack")
        return {"confirmed": ids, "already_confirmed": [], "unknown": []}

    def write_lease(**kwargs):
        order.append("lease")
        leases.append(kwargs)

    # Act
    delivered = asyncio.run(
        ingress.drain_once(
            name="scholar",
            turn_url="direct://resident",
            bearer="secret",
            poll_notifications=poll,
            ack_notifications=ack,
            deliver=deliver,
            card_lease_writer=write_lease,
        )
    )
    # Assert
    assert (delivered, order, leases) == (
        1,
        ["lease", "ack"],
        [
            {
                "agent": "scholar",
                "card_id": "card-1",
                "role": "reviewer",
                "expires_at": 200.0,
            }
        ],
    )


def test_failed_terminal_delivery_leaves_cards_notification_unacked():
    # Arrange
    calls = []

    def poll(_agent, **_kwargs):
        return {
            "store": "postgresql://cards-primary",
            "unconfirmed": ["m_retry"],
            "notifications": [
                {
                    "id": "m_retry",
                    "event_type": "dm",
                    "actor": "operator",
                    "body": "do not lose me",
                }
            ],
        }

    async def fail_delivery(_event, **_kwargs):
        raise RuntimeError("terminal did not visibly render the message")

    def ack(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    # Act
    delivered = asyncio.run(
        ingress.drain_once(
            name="scitex-hub",
            turn_url="http://127.0.0.1:19001/v1/turn",
            bearer="secret",
            poll_notifications=poll,
            ack_notifications=ack,
            deliver=fail_delivery,
        )
    )
    # Assert
    assert (delivered, calls) == (0, [])


def test_one_cards_exchange_survives_202_visible_turn_and_final_ack():
    # Arrange: this is the cross-boundary contract. Cards has already persisted
    # the notification and its responder-issued 202 exchange before SAC polls.
    exchange_id = "xch_20260912T000000Z_cards_abcdef"
    opened_at = "2026-09-12T00:00:00+00:00"
    exchanges = {
        exchange_id: {
            "kind": "http",
            "code": 202,
            "message": "Cards accepted the DM; poll this exchange",
        }
    }
    transcript = []
    acknowledgements = []
    opened_exchange_ids = []
    finished_exchange_ids = []

    def open_existing(**kwargs):
        opened_exchange_ids.append(kwargs["exchange_id"])
        return exchange_id, opened_at

    def finish_existing(received_id, **kwargs):
        finished_exchange_ids.append(received_id)
        status = kwargs["status"]
        exchanges[received_id] = {
            "kind": status.kind,
            "code": status.code,
            "message": status.message,
        }

    def visible_turn(text, **kwargs):
        transcript.append((text, kwargs))
        return True

    server = turn_bridge.build_server(
        host="127.0.0.1",
        port=0,
        on_turn=visible_turn,
        agent_name="scitex-hub",
        exchange_open=open_existing,
        exchange_finish=finish_existing,
        exchange_read=exchanges.get,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def poll(_agent, **_kwargs):
        return {
            "store": "cards-primary",
            "unconfirmed": ["n_visible"],
            "notifications": [
                {
                    "id": "n_visible",
                    "msg_id": "m_visible",
                    "exchange_id": exchange_id,
                    "event_type": "dm",
                    "actor": "operator",
                    "body": "Please inspect signup.",
                }
            ],
        }

    def ack(agent, ids, **kwargs):
        acknowledgements.append((agent, ids, kwargs))
        return {"confirmed": ids, "already_confirmed": [], "unknown": []}

    try:
        # Act
        delivered = asyncio.run(
            ingress.drain_once(
                name="scitex-hub",
                turn_url=f"http://127.0.0.1:{server.server_address[1]}/v1/turn",
                bearer=None,
                poll_notifications=poll,
                ack_notifications=ack,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # Assert
    assert (
        delivered,
        exchanges[exchange_id]["code"],
        set(exchanges),
        "operator" in transcript[0][0],
        "Please inspect signup." in transcript[0][0],
        acknowledgements,
        opened_exchange_ids,
        finished_exchange_ids,
    ) == (
        1,
        200,
        {exchange_id},
        True,
        True,
        [("scitex-hub", ["n_visible"], {"store": "cards-primary"})],
        [exchange_id],
        [exchange_id],
    )


def test_doorbells_coalesce_at_serial_durable_poll_boundary():
    # Arrange: two hints may accumulate while a turn runs. They carry no data;
    # the first poll drains the inbox and the stale second poll finds nothing.
    calls = []
    active = 0
    peak_active = 0
    outcomes = iter([2, 0])

    class Watch:
        def __enter__(self):
            return iter(
                [
                    {"recipient_id": "scitex-hub", "hint": "poll_notifications"},
                    {"recipient_id": "scitex-hub", "hint": "poll_notifications"},
                ]
            )

        def __exit__(self, *_args):
            return None

    def watch(agent, **kwargs):
        calls.append(("watch", agent, kwargs))
        return Watch()

    async def drain(**kwargs):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0)
        active -= 1
        calls.append(("drain", kwargs))
        return next(outcomes)

    # Act
    delivered = asyncio.run(
        ingress._watch_cycle(
            name="scitex-hub",
            turn_url="http://127.0.0.1:19001/v1/turn",
            bearer=None,
            store="postgresql://cards-primary/cards",
            timeout_s=90,
            watch_notifications=watch,
            drain=drain,
        )
    )

    # Assert
    assert (
        delivered,
        peak_active,
        [kind for kind, *_rest in calls],
        calls[0][2]["store"],
        [call[1]["store"] for call in calls if call[0] == "drain"],
    ) == (
        2,
        1,
        ["watch", "drain", "drain"],
        "postgresql://cards-primary/cards",
        ["postgresql://cards-primary/cards"] * 2,
    )
