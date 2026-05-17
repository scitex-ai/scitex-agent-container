"""Inbox-driven SDK conversation, with optional supervisor restarts.

Split out of :mod:`._claude_session` so the runner module stays under
the project's per-file line cap. The public entry point here is
:func:`run_conversation`, which holds one ``ClaudeSDKClient`` open and
drains turn envelopes from an asyncio queue. When ``max_restarts > 0``
it also acts as a **supervisor**: if the SDK client dies mid-session
(network blip, SDK panic, etc.) we log the error, sleep with
exponential backoff, refresh the resume session_id from disk so the
next attempt picks up where the last completed turn left off, and
re-open the client. Init failures (missing SDK, bad options) are
treated as terminal — no point retrying a config error.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ._session_state import (
    STATE_IDLE,
    STATE_WORKING,
    accumulate_quota,
    append_session_message,
    read_session_id,
    write_heartbeat,
    write_session_id,
)

logger = logging.getLogger(__name__)


def _safe_repr(value: object) -> str:
    """Bounded repr so a runaway tool-result blob can't bloat session.jsonl."""
    s = repr(value)
    return s if len(s) <= 1024 else s[:1024] + "…"


def _drain_failed_inbox(inbox: "asyncio.Queue", exc: BaseException) -> None:
    """Resolve pending turn futures with the failure so producers don't hang."""
    from ._session_inbox import TurnEnvelope

    while not inbox.empty():
        try:
            env = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_exception(exc)


async def _drive_turn(
    client: Any,
    env: Any,
    *,
    state_dir: Path,
    pid: int,
    stop: asyncio.Event,
    print_stream: bool,
    sdk_types: dict,
) -> None:
    AssistantMessage = sdk_types["AssistantMessage"]
    TextBlock = sdk_types["TextBlock"]
    UserMessage = sdk_types["UserMessage"]
    ResultMessage = sdk_types["ResultMessage"]

    write_heartbeat(state_dir, pid=pid, state=STATE_WORKING)
    append_session_message(state_dir, {"type": "user", "text": env.text})
    chunks: list[str] = []
    try:
        await client.query(env.text)
        async for msg in client.receive_response():
            if stop.is_set():
                await client.interrupt()
                break
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                        append_session_message(
                            state_dir,
                            {"type": "assistant", "text": block.text},
                        )
                        if print_stream:
                            sys.stdout.write(block.text)
                            sys.stdout.flush()
            elif isinstance(msg, UserMessage):
                append_session_message(
                    state_dir,
                    {"type": "user_echo", "raw": _safe_repr(msg)},
                )
            elif isinstance(msg, ResultMessage):
                sid = getattr(msg, "session_id", None)
                if sid:
                    write_session_id(state_dir, sid)
                    # Tag the envelope so the HTTP sidecar can echo the
                    # SDK session id back to the caller. Set BEFORE the
                    # future resolves (in the finally block below) so
                    # the awaiter sees a consistent (text, session_id).
                    env.session_id = sid
                usage = getattr(msg, "usage", None)
                accumulate_quota(state_dir, usage)
                append_session_message(
                    state_dir,
                    {"type": "result", "session_id": sid, "usage": usage},
                )
                break
    finally:
        if not env.response.done():
            env.response.set_result("".join(chunks))
        if not stop.is_set():
            write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)


async def run_conversation(
    name: str,
    state_dir: Path,
    *,
    pid: int,
    inbox: "asyncio.Queue",
    resume_session_id: str | None,
    stop: asyncio.Event,
    print_stream: bool = False,
    max_restarts: int = 0,
    restart_backoff_s: float = 1.0,
    sdk_module: Any | None = None,
    build_sdk_options_fn: Any | None = None,
) -> None:
    """Drive an inbox-driven conversation against ``ClaudeSDKClient``.

    With ``max_restarts == 0`` (default) a single SDK failure terminates
    the runner — matches the pre-supervisor behaviour. With
    ``max_restarts > 0`` an SDK runtime exception is logged, the inbox
    is preserved (so queued turns survive the restart), and the client
    is reopened after ``restart_backoff_s * 2**attempt`` seconds.
    """
    from ._session_inbox import ShutdownEnvelope, TurnEnvelope

    try:
        if sdk_module is None:
            import claude_agent_sdk as sdk_module  # type: ignore[no-redef]
        AssistantMessage = sdk_module.AssistantMessage
        ClaudeSDKClient = sdk_module.ClaudeSDKClient
        HookMatcher = sdk_module.HookMatcher
        ResultMessage = sdk_module.ResultMessage
        TextBlock = sdk_module.TextBlock
        UserMessage = sdk_module.UserMessage
    except Exception as exc:  # stx-allow: fallback (reason: optional dep import — record + drain rather than crash the runner)
        logger.error("claude-agent-sdk import failed: %s", exc)
        append_session_message(
            state_dir, {"type": "error", "kind": "sdk_missing", "detail": str(exc)}
        )
        _drain_failed_inbox(inbox, RuntimeError(f"sdk import: {exc}"))
        return

    sdk_types = {
        "AssistantMessage": AssistantMessage,
        "TextBlock": TextBlock,
        "UserMessage": UserMessage,
        "ResultMessage": ResultMessage,
    }

    from ..runtimes._sdk_common import SDKCommonError, build_sdk_options
    from ._session_hooks import build_event_log_hooks

    if build_sdk_options_fn is None:
        build_sdk_options_fn = build_sdk_options

    hooks = build_event_log_hooks(name, HookMatcher)

    attempt = 0
    last_exc: BaseException | None = None
    while True:
        # Re-read session_id each iteration so a supervised restart resumes
        # against the most recent completed turn rather than the initial sid.
        current_sid = read_session_id(state_dir) or resume_session_id
        try:
            options = build_sdk_options_fn(
                name,
                permission_mode="bypassPermissions",
                resume=current_sid,
                hooks=hooks,
            )
        except SDKCommonError as exc:
            logger.error("could not build sdk options: %s", exc)
            append_session_message(
                state_dir, {"type": "error", "kind": "options", "detail": str(exc)}
            )
            _drain_failed_inbox(inbox, exc)
            return

        try:
            async with ClaudeSDKClient(options=options) as client:
                while True:
                    env = await inbox.get()
                    if isinstance(env, ShutdownEnvelope):
                        return
                    if not isinstance(env, TurnEnvelope):
                        continue
                    await _drive_turn(
                        client,
                        env,
                        state_dir=state_dir,
                        pid=pid,
                        stop=stop,
                        print_stream=print_stream,
                        sdk_types=sdk_types,
                    )
                    if env.exit_after:
                        stop.set()
                        return
                    if stop.is_set():
                        return
        except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad; supervisor decides retry vs terminate)
            last_exc = exc
            logger.exception("claude-session conversation failed for %s", name)
            append_session_message(
                state_dir,
                {
                    "type": "error",
                    "kind": "sdk_runtime",
                    "detail": str(exc),
                    "attempt": attempt,
                },
            )
            if attempt >= max_restarts or stop.is_set():
                _drain_failed_inbox(inbox, exc)
                return
            delay = restart_backoff_s * (2**attempt)
            append_session_message(
                state_dir,
                {"type": "supervisor", "event": "restarting", "in_s": delay},
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                # stop fired during backoff → don't restart
                _drain_failed_inbox(inbox, last_exc)
                return
            except asyncio.TimeoutError:
                pass
            attempt += 1
