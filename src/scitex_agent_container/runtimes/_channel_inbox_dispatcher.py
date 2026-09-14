"""Dispatch durable SAC and Cards envelopes to the selected harness adapter."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .._mcp._channel_sse import _consume_sse
from .._mcp.channel import _push_channel_event
from ._apptainer_build import _read_listen_bearer
from ._cards_ingress import consume as consume_cards

log = logging.getLogger(__name__)


class _NotificationSink:
    async def send_message(self, message: Any) -> None:
        del message


async def consume(
    *,
    name: str,
    listen_url: str,
    turn_url: str,
    channels: tuple[str, ...] = ("server:sac", "server:scitex-cards"),
    bearer: str | None = None,
    environment: Mapping[str, str] = os.environ,
    resolve_bearer: Callable[[], str | None] = _read_listen_bearer,
    consume_sse: Callable[..., Awaitable[None]] = _consume_sse,
    consume_cards_notifications: Callable[..., Awaitable[None]] = consume_cards,
    push_event: Callable[..., Awaitable[None]] = _push_channel_event,
    dispatch_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """Consume declared durable rails and ACK only proven target admission.

    The daemon owns transport consumption.  ``/v1/turn`` is the stable target
    boundary: the selected runtime behind it decides how to apply a new turn
    or a busy-session steer.  Thus Cards and SAC never need to know whether
    Claude Code, Hermes, or Codex owns the session.
    """
    bearer = bearer or environment.get("SAC_LISTEN_BEARER") or resolve_bearer()
    if not bearer:
        raise RuntimeError("SAC listen bearer is required for channel inbox delivery")
    sink = _NotificationSink()

    async def default_dispatch(event: dict[str, Any]) -> None:
        event = dict(event)
        event["_require_terminal_visibility"] = True
        await push_event(
            sink,
            event,
            agent_name=name,
            listen_url=listen_url,
            bearer=bearer,
            turn_url=turn_url,
        )

    target_dispatch = dispatch_event or default_dispatch

    async def on_event(event: dict[str, Any]) -> None:
        event = dict(event)
        event["_require_terminal_visibility"] = True
        await target_dispatch(event)

    async def deliver_cards(event: dict[str, Any], **_transport: Any) -> None:
        await on_event(event)

    consumers: list[Awaitable[None]] = []
    if "server:sac" in channels:
        inbox_url = f"{listen_url.rstrip('/')}/agents/{name}/inbox"
        consumers.append(
            consume_sse(
                f"{inbox_url}/stream?ack=explicit",
                bearer,
                on_event,
                ack_url=f"{inbox_url}/ack",
            )
        )
    if "server:scitex-cards" in channels:
        consumers.append(
            consume_cards_notifications(
                name=name,
                turn_url=turn_url,
                bearer=bearer,
                deliver=deliver_cards,
            )
        )
    if not consumers:
        raise RuntimeError("channel inbox dispatcher has no durable rail to consume")
    await asyncio.gather(*consumers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--listen-url", required=True)
    parser.add_argument("--turn-url", required=True)
    parser.add_argument("--config-path", required=True, type=Path)
    parser.add_argument("--process-role", required=True)
    parser.add_argument("--incarnation-id", required=True)
    parser.add_argument("--channel", action="append", dest="channels", default=[])
    args = parser.parse_args(argv)
    asyncio.run(
        consume(
            name=args.name,
            listen_url=args.listen_url,
            turn_url=args.turn_url,
            channels=tuple(args.channels),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
