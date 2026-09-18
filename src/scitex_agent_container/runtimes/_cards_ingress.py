"""Translate and durably deliver SciTeX Cards notifications.

This module owns the Cards source adapter only.  It knows how to poll and
confirm Cards notifications, but it deliberately knows nothing about the
selected agent harness.  The channel inbox dispatcher supplies the delivery
callable and confirms a Cards row only after that target adapter returns a
positive receipt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from scitex_dev.status import Check, StatusCode

from .._mcp._channel_wake import _wake_turn

log = logging.getLogger(__name__)
DEFAULT_RECONCILE_INTERVAL_S = 2.0


def canonical_store_dsn(environ: Mapping[str, str]) -> str:
    """Return the one supported shared-state target or fail without leaking it."""
    store = environ.get("SCITEX_STORE_DSN")
    if not store:
        raise RuntimeError(
            "Cards ingress requires SCITEX_STORE_DSN; configure the "
            "canonical shared PostgreSQL store on port 55432 and restart the agent"
        )
    try:
        target = urlsplit(store)
        port = target.port
    except ValueError as exc:
        raise RuntimeError(
            "SCITEX_STORE_DSN is malformed; configure the canonical shared "
            "PostgreSQL store on port 55432 and restart the agent"
        ) from exc
    if target.scheme not in {"postgres", "postgresql"} or not target.hostname:
        raise RuntimeError(
            "SCITEX_STORE_DSN must name the canonical shared PostgreSQL store"
        )
    if port != 55432:
        raise RuntimeError(
            "SCITEX_STORE_DSN must use the canonical shared PostgreSQL port 55432"
        )
    return store


def _log_check(
    level: int, check: Check, *, agent: str, notification_id: str = ""
) -> None:
    """Emit the shared three-valued diagnostic shape, never a local taxonomy."""
    log.log(
        level,
        "cards ingress agent=%s notification_id=%s check=%s",
        agent,
        notification_id or "-",
        json.dumps(check.to_dict(), sort_keys=True),
    )


def event_from_notification(record: dict[str, Any]) -> dict[str, Any]:
    """Translate one Cards pull-inbox row to SAC's neutral channel envelope."""
    event_type = str(record.get("event_type") or "notification")
    source = str(record.get("actor") or "scitex-cards")
    card_id = str(record.get("card_id") or "")
    notification_id = str(record.get("id") or "")
    event: dict[str, Any] = {
        "msg_id": str(record.get("msg_id") or notification_id),
        "cards_notification_id": notification_id,
        "kind": "message",
        "from_agent": source,
        "content": str(record.get("body") or ""),
    }
    if event_type == "dm" and card_id:
        event["conversation_id"] = card_id
    if card_id:
        event["card_id"] = card_id
    lease_role = str(record.get("lease_role") or "").strip()
    lease_expires_at = record.get("lease_expires_at")
    if card_id and lease_role and isinstance(lease_expires_at, (int, float)):
        event["_card_lease"] = {
            "card_id": card_id,
            "role": lease_role,
            "expires_at": float(lease_expires_at),
        }
    exchange_id = record.get("exchange_id")
    if isinstance(exchange_id, str) and exchange_id:
        # Newer Cards producers mint this at the persistence boundary. Carry
        # the responder-issued handle unchanged through SAC and the selected
        # harness adapter; never
        # substitute a local success id for a sender-visible exchange.
        event["exchange_id"] = exchange_id
    event["_persisted"] = True
    return event


def _cards_api() -> tuple[Callable[..., dict], Callable[..., dict], Callable[..., Any]]:
    try:
        from scitex_cards import (
            ack_notifications,
            poll_notifications,
            watch_notifications,
        )
    except Exception as exc:
        raise RuntimeError(
            "SciTeX Cards ingress is unavailable: install scitex-cards in the "
            "SAC control environment; persisted Cards notifications remain "
            "unacknowledged and safe to retry."
        ) from exc
    return poll_notifications, ack_notifications, watch_notifications


async def drain_once(
    *,
    name: str,
    turn_url: str,
    bearer: str | None,
    store: str | None = None,
    poll_notifications: Callable[..., dict] | None = None,
    ack_notifications: Callable[..., dict] | None = None,
    deliver: Callable[..., Awaitable[None]] = _wake_turn,
    card_lease_writer: Callable[..., None] | None = None,
) -> int:
    """Deliver and confirm a batch; never ACK before target visibility."""
    if poll_notifications is None or ack_notifications is None:
        default_poll, default_ack, _default_watch = _cards_api()
        poll_notifications = poll_notifications or default_poll
        ack_notifications = ack_notifications or default_ack
    lease_writer = card_lease_writer
    if lease_writer is None:
        from .._lifecycle._session_movement import resolve_state_dir
        from .._state.authoritative_heartbeat import write_card_lease

        def _default_card_lease_writer(**kwargs: Any) -> None:
            state_dir = resolve_state_dir(str(kwargs.pop("agent")))
            if state_dir is None:
                raise RuntimeError(
                    "Cards lease cannot be confirmed: agent state directory is absent"
                )
            write_card_lease(state_dir, **kwargs)

        lease_writer = _default_card_lease_writer

    payload = await asyncio.to_thread(
        # Read the full view and select ``unconfirmed`` below.  Cards' legacy
        # A legacy harness channel can mark a record seen after writing a
        # notification that the target ignores; unseen-only would hide that durable, invisible
        # notification forever.
        partial(
            poll_notifications,
            name,
            unseen_only=False,
            ack=False,
            store=store,
        )
    )
    store = payload.get("store")
    records = payload.get("notifications") or []
    unconfirmed = set(payload.get("unconfirmed") or [])
    delivered_count = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        event = event_from_notification(record)
        notification_id = event["cards_notification_id"]
        if notification_id not in unconfirmed:
            continue
        if not notification_id or not event["content"].strip():
            _log_check(
                logging.ERROR,
                Check.not_ok(
                    "notification_valid",
                    "the persisted Cards notification has a blank id or body",
                    "repair the notification producer, then leave this row unconfirmed",
                ),
                agent=name,
            )
            continue
        try:
            await deliver(event, turn_url=turn_url, bearer=bearer)
        except Exception as exc:
            _log_check(
                logging.WARNING,
                Check.unknown(
                    "harness_delivery_visible",
                    "the dispatcher did not establish target-harness visibility "
                    f"({type(exc).__name__})",
                    "leave the Cards notification unconfirmed; inspect "
                    f"`sac agents logs {name}` before the durable retry",
                    cause=StatusCode(
                        kind="http",
                        code=502,
                        message=(
                            "target-harness visibility could not be established; inspect "
                            f"`sac agents logs {name}`"
                        ),
                    ),
                ),
                agent=name,
                notification_id=notification_id,
            )
            continue

        lease = event.get("_card_lease")
        if isinstance(lease, dict):
            lease_writer(agent=name, **lease)
        receipt = await asyncio.to_thread(
            partial(ack_notifications, name, [notification_id], store=store)
        )
        accepted = set(receipt.get("confirmed") or []) | set(
            receipt.get("already_confirmed") or []
        )
        if notification_id not in accepted or notification_id in set(
            receipt.get("unknown") or []
        ):
            _log_check(
                logging.ERROR,
                Check.not_ok(
                    "cards_confirmation_recorded",
                    "Cards did not confirm the terminal-visible notification id",
                    "verify that poll_notifications and ack_notifications resolve the "
                    "same Cards store, then retry confirmation for this id",
                ),
                agent=name,
                notification_id=notification_id,
            )
            continue
        delivered_count += 1
    return delivered_count


async def _watch_cycle(
    *,
    name: str,
    turn_url: str,
    bearer: str | None,
    store: str | None,
    timeout_s: float,
    watch_notifications: Callable[..., Any],
    drain: Callable[..., Awaitable[int]] = drain_once,
    deliver: Callable[..., Awaitable[None]] = _wake_turn,
) -> int:
    """Drain serially after doorbells; stale hints cannot synthesize turns."""
    manager = await asyncio.to_thread(
        partial(watch_notifications, name, timeout=timeout_s, store=store)
    )
    events = await asyncio.to_thread(manager.__enter__)
    delivered = 0
    try:
        while True:
            hint = await asyncio.to_thread(next, events, None)
            if hint is None:
                return delivered
            # A hint carries no message data. Polling the durable inbox is the
            # only way a turn starts; stale/coalesced hints whose poll is empty
            # therefore start no turn.
            delivered += await drain(
                name=name,
                turn_url=turn_url,
                bearer=bearer,
                store=store,
                deliver=deliver,
            )
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


async def consume(
    *,
    name: str,
    turn_url: str,
    bearer: str | None,
    reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S,
    watch_notifications: Callable[..., Any] | None = None,
    deliver: Callable[..., Awaitable[None]] = _wake_turn,
) -> None:
    """Drain durably on a bounded cadence; use LISTEN only as an accelerator."""
    if watch_notifications is None:
        _poll, _ack, watch_notifications = _cards_api()
    store = canonical_store_dsn(os.environ)
    watch_impaired = False
    while True:
        try:
            await drain_once(
                name=name,
                turn_url=turn_url,
                bearer=bearer,
                store=store,
                deliver=deliver,
            )
        except Exception as exc:
            _log_check(
                logging.WARNING,
                Check.unknown(
                    "cards_notifications_readable",
                    f"the Cards notification poll raised {type(exc).__name__}",
                    "run `scitex-cards health`, repair the reported store or dependency, "
                    "then let the durable poll retry",
                ),
                agent=name,
            )
        timeout_s = reconcile_interval_s * random.uniform(0.8, 1.2)
        try:
            await _watch_cycle(
                name=name,
                turn_url=turn_url,
                bearer=bearer,
                store=store,
                timeout_s=timeout_s,
                watch_notifications=watch_notifications,
                deliver=deliver,
            )
            if watch_impaired:
                _log_check(
                    logging.INFO,
                    Check.ok(
                        "cards_notification_watch",
                        "the Cards doorbell accepted a complete watch cycle",
                        hint="continue the bounded durable reconcile sweep",
                    ),
                    agent=name,
                )
                watch_impaired = False
        except Exception as exc:
            status = getattr(exc, "status", None)
            cause = status if isinstance(status, StatusCode) else None
            if not watch_impaired:
                _log_check(
                    logging.WARNING,
                    Check.unknown(
                        "cards_notification_watch",
                        f"the Cards doorbell is unavailable ({type(exc).__name__})",
                        "durable notifications remain safe; run `scitex-cards health "
                        "--json` while SAC reconciles the durable inbox every "
                        f"{reconcile_interval_s:g} seconds",
                        cause=cause,
                    ),
                    agent=name,
                )
                watch_impaired = True
            await asyncio.sleep(timeout_s)


__all__ = [
    "DEFAULT_RECONCILE_INTERVAL_S",
    "canonical_store_dsn",
    "consume",
    "drain_once",
    "event_from_notification",
]
