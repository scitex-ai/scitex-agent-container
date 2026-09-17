"""Runtime worker for persistent, bounded missing-agentic-ACK nudges."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import uuid
from typing import Any

from .._state.dispatch_ledger import get_dispatch
from .._state.dispatch_nudges import (
    NudgeRecord,
    PostgresNudgeRepository,
    schedule_nudge,
    tick_nudges,
)

log = logging.getLogger(__name__)


def _seconds(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def schedule_dispatch_nudge(*, agent: str, dispatch_id: str, target: str) -> None:
    """Persist nudge timing on successful visible delivery; idempotent."""
    schedule_nudge(
        PostgresNudgeRepository(),
        agent=agent,
        dispatch_id=dispatch_id,
        target=target,
        now=time.time(),
        initial_delay_s=_seconds("SAC_AGENTIC_ACK_NUDGE_INITIAL_S", 30.0),
        deadline_s=_seconds("SAC_AGENTIC_ACK_DEADLINE_S", 900.0),
    )


def build_nudge_payload(record: NudgeRecord) -> dict[str, Any]:
    """Build only a lightweight nonce reminder, never the original task body."""
    content = (
        "Agentic ACK reminder: after understanding the request bound to dispatch_id "
        f"{record.dispatch_id}, invoke a2a_agentic_ack with that exact nonce, "
        "an understood summary, owner, and next checkpoint. This reminder does not "
        "repeat the original task."
    )
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": {
                "from_agent": record.agent,
                "kind": "agentic_ack_nudge",
                "extra": {"dispatch_id": record.dispatch_id},
            },
        },
    }


def _send_nudge(
    record: NudgeRecord, *, listen_url: str, bearer: str | None
) -> None:
    import httpx

    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    response = httpx.post(
        f"{listen_url.rstrip('/')}/agents/{record.target}/message:send",
        json=build_nudge_payload(record),
        headers=headers,
        timeout=10.0,
    )
    response.raise_for_status()


def _escalate_operator(record: NudgeRecord) -> None:
    message = (
        "A2A semantic ACK deadline exceeded: "
        f"{record.agent} -> {record.target}, dispatch_id={record.dispatch_id}, "
        f"attempts={record.attempts}. Transport may have delivered, but recipient "
        "understanding remains unproven."
    )
    executable = shutil.which("scitex-notification")
    if executable is None:
        raise RuntimeError(
            "scitex-notification is unavailable; operator escalation not delivered"
        )
    subprocess.run(
        [executable, "send", message],
        check=True,
        timeout=15.0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_nudge_tick(*, agent: str, listen_url: str, bearer: str | None) -> None:
    """Run one durable scheduler tick; callback failures are loud and bounded."""
    repo = PostgresNudgeRepository()

    def status_of(dispatch_id: str) -> str | None:
        row = get_dispatch(dispatch_id, agent=agent)
        return str(row.get("status")) if row is not None else None

    def send(record: NudgeRecord) -> None:
        try:
            _send_nudge(record, listen_url=listen_url, bearer=bearer)
        except Exception as exc:  # stx-allow: fallback (reason: nudge attempt is pre-persisted for restart dedup; one HTTP failure must not kill the scheduler)
            log.warning("agentic-ACK nudge failed for %s: %s", record.dispatch_id, exc)

    def escalate(record: NudgeRecord) -> None:
        try:
            _escalate_operator(record)
        except Exception as exc:  # stx-allow: fallback (reason: escalation is pre-persisted; report notification backend failure without killing the scheduler)
            log.error(
                "agentic-ACK operator escalation failed for %s: %s",
                record.dispatch_id,
                exc,
            )

    tick_nudges(
        repo,
        agent=agent,
        now=time.time(),
        status_of=status_of,
        send_nudge=send,
        escalate=escalate,
        initial_delay_s=_seconds("SAC_AGENTIC_ACK_NUDGE_INITIAL_S", 30.0),
        max_delay_s=_seconds("SAC_AGENTIC_ACK_NUDGE_MAX_S", 300.0),
    )


async def run_nudge_scheduler(
    *, agent: str, listen_url: str, bearer: str | None
) -> None:
    """Periodically tick off-loop; cancellation stops promptly."""
    poll_s = _seconds("SAC_AGENTIC_ACK_NUDGE_POLL_S", 5.0)
    while True:
        await asyncio.sleep(poll_s)
        try:
            await asyncio.to_thread(
                run_nudge_tick,
                agent=agent,
                listen_url=listen_url,
                bearer=bearer,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # stx-allow: fallback (reason: store outage is logged to the MCP process stderr each bounded poll and must not kill the MCP server)
            log.warning("agentic-ACK nudge scheduler tick failed: %s", exc)


__all__ = [
    "build_nudge_payload",
    "run_nudge_scheduler",
    "run_nudge_tick",
    "schedule_dispatch_nudge",
]
