"""The vendor-neutral per-turn event pump shared by every turn driver.

Card ``sac-codex-python-sdk-harness-20260814``. Extracted VERBATIM from
:func:`._openai_turn_driver._drive_openai_turn` when the fourth harness
arrived and the function turned out to be openai-specific in NAME only:
every line of it is a function of :class:`~._harness_session.NormalizedEvent`
kinds and the daemon's own bookkeeping surface, and it owes nothing to
``openai-agents``. Copying it for codex would have duplicated the
transcript format, the beat protocol and the error contract — three
things that must not be allowed to drift between harnesses.

WHAT IT DOES, once per turn:

* stamps a BUSY beat as :data:`~._incarnation.WRITER_TURN_DRIVER` (the
  self-testimony the daemon's periodic loop preserves rather than
  overwriting — see :func:`._daemon_contract.make_daemon_state_fn`);
* appends the user turn and then every event to ``session.jsonl``;
* accumulates the terminal result's usage into the quota totals and
  records the harness's session id on the envelope, so the a2a caller
  gets a consistent ``(text, session_id)`` pair;
* resolves ``env.response`` exactly once — with the joined assistant
  text, or with the failure when the turn yielded ``kind="error"``;
* stamps a READY beat once the turn closes.

WHAT IT DOES NOT DO: touch the pid file, the periodic heartbeat loop,
the a2a sidecar or ``exit.json``. Those are the daemon's.

An EXCEPTION out of ``session.send`` is outside the HarnessSession
contract (errors are supposed to travel as turn-ending ``kind="error"``
events), so it propagates to the daemon, whose done-callback records
``crashed`` honestly instead of this pump papering over it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ._harness_session import Message
from ._incarnation import WRITER_TURN_DRIVER
from ._session_state import (
    STATE_BUSY,
    STATE_READY,
    accumulate_quota,
    append_session_message,
    report_sdk_error,
    write_heartbeat,
)
from ._session_turn import _safe_repr

logger = logging.getLogger(__name__)

__all__ = ["drive_harness_turn"]


async def drive_harness_turn(
    session: Any,
    env: Any,
    *,
    state_dir: Path,
    pid: int,
    stop: asyncio.Event,
    print_stream: bool,
    name: str,
    host: str | None,
    harness: str = "",
) -> None:
    """Run ONE turn against ``session``, resolving ``env.response``.

    ``session`` is any :class:`~._harness_session.HarnessSession` — the
    pump only calls :meth:`send` and reads ``NormalizedEvent`` fields.
    ``harness`` names the harness in log lines and in the transcript's
    error records, so a mixed-harness state dir stays readable; it is
    presentation only and never branches behaviour.
    """
    write_heartbeat(
        state_dir,
        pid=pid,
        state=STATE_BUSY,
        name=name,
        host=host,
        writer=WRITER_TURN_DRIVER,
    )
    append_session_message(state_dir, {"type": "user", "text": env.text})
    chunks: list[str] = []
    error_detail: str | None = None
    try:
        async for event in session.send(Message(role="user", content=env.text)):
            if stop.is_set():
                break
            if event.kind == "text_delta":
                chunks.append(event.text)
                append_session_message(
                    state_dir, {"type": "assistant", "text": event.text}
                )
                if print_stream:
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
            elif event.kind == "tool_call":
                append_session_message(
                    state_dir,
                    {
                        "type": "tool_call",
                        "tool": event.tool_name,
                        "input": _safe_repr(event.tool_input),
                    },
                )
            elif event.kind == "tool_result":
                append_session_message(
                    state_dir,
                    {"type": "tool_result", "output": _safe_repr(event.tool_output)},
                )
            elif event.kind in ("reasoning", "task"):
                append_session_message(
                    state_dir, {"type": event.kind, "text": event.text}
                )
            elif event.kind == "result":
                result = event.result
                sid = getattr(result, "session_id", None)
                if sid:
                    # Echoed to the /v1/turn caller by the sidecar; set
                    # BEFORE the future resolves (finally below) so the
                    # awaiter sees a consistent (text, session_id).
                    env.session_id = sid
                usage = dict(getattr(result, "usage", None) or {})
                accumulate_quota(state_dir, usage)
                append_session_message(
                    state_dir,
                    {"type": "result", "session_id": sid, "usage": usage},
                )
            elif event.kind == "error":
                error_detail = str(event.error)
                logger.error(
                    "%s turn failed for %s: %s", harness or "harness", name, error_detail
                )
                append_session_message(
                    state_dir,
                    {"type": "error", "kind": "harness_turn", "detail": error_detail},
                )
                if host:
                    report_sdk_error(
                        name=name,
                        host=host,
                        cause="harness-turn",
                        detail=error_detail,
                    )
                break
        if error_detail is not None and not env.response.done():
            env.response.set_exception(RuntimeError(error_detail))
    finally:
        if not env.response.done():
            env.response.set_result("".join(chunks))
        if not stop.is_set():
            write_heartbeat(
                state_dir,
                pid=pid,
                state=STATE_READY,
                name=name,
                host=host,
                writer=WRITER_TURN_DRIVER,
            )
