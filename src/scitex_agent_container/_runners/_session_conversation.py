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
    record_turn_transition,
    report_sdk_error,
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
    name: str | None = None,
    host: str | None = None,
    db_writer=None,
) -> None:
    AssistantMessage = sdk_types["AssistantMessage"]
    TextBlock = sdk_types["TextBlock"]
    UserMessage = sdk_types["UserMessage"]
    ResultMessage = sdk_types["ResultMessage"]

    write_heartbeat(
        state_dir,
        pid=pid,
        state=STATE_WORKING,
        name=name,
        host=host,
        db_writer=db_writer,
    )
    append_session_message(state_dir, {"type": "user", "text": env.text})
    # Tag a turn_id on the envelope so the diary's four
    # state-transition rows share an identity. ``record_turn_transition``
    # is a no-op when name/host/turn_id are unset (legacy callers).
    turn_id = getattr(env, "turn_id", None)
    if turn_id and name and host:
        record_turn_transition(
            turn_id=turn_id,
            name=name,
            host=host,
            status="delivered",
            prompt_text=env.text,
            db_writer=db_writer,
        )
        record_turn_transition(
            turn_id=turn_id,
            name=name,
            host=host,
            status="read",
            db_writer=db_writer,
        )
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
        if turn_id and name and host:
            record_turn_transition(
                turn_id=turn_id,
                name=name,
                host=host,
                status="responded",
                response_text="".join(chunks),
                session_id=getattr(env, "session_id", None),
                db_writer=db_writer,
            )
        if not stop.is_set():
            write_heartbeat(
                state_dir,
                pid=pid,
                state=STATE_IDLE,
                name=name,
                host=host,
                db_writer=db_writer,
            )


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
    host: str | None = None,
    db_writer=None,
    channels: list[str] | None = None,
    a2a_port: int | None = None,
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
        if host:
            report_sdk_error(
                name=name,
                host=host,
                cause="sdk-missing",
                detail=str(exc),
                db_writer=db_writer,
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

    # Thread spec.claude.channels + the runner's own a2a_port into the
    # SDK options under the sac-private ``extra`` keys so build_sdk_options
    # auto-registers the ``sac mcp channel`` stdio MCP when channels
    # contains ``server:sac`` (see runtimes/_sdk_common.py). Without this
    # the long-lived daemon session never subscribes to its inbox SSE and
    # ``a2a_send`` to it yields delivered_subscriber_count=0. Mirrors the
    # legacy stateless path in a2a/_handlers.py.
    sdk_extra: dict | None = None
    if channels or a2a_port is not None:
        sdk_extra = {}
        if channels:
            sdk_extra["_channels"] = list(channels)
        if a2a_port is not None:
            sdk_extra["_a2a_port"] = int(a2a_port)

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
                extra=sdk_extra,
            )
        except SDKCommonError as exc:
            # A missing/expired credentials file fails option-building
            # with the refresh hint already in the message; classify it
            # as auth-expired so the operator-facing record names the
            # cause rather than the generic ``sdk-options``.
            from ._auth_failure import (
                AUTH_FAILURE_CAUSE,
                classify_auth_failure,
            )

            auth_detail = classify_auth_failure(exc)
            if auth_detail is not None:
                opt_cause, opt_detail = AUTH_FAILURE_CAUSE, auth_detail
            else:
                opt_cause, opt_detail = "sdk-options", str(exc)
            logger.error("could not build sdk options: %s", opt_detail)
            append_session_message(
                state_dir, {"type": "error", "kind": "options", "detail": opt_detail}
            )
            if host:
                report_sdk_error(
                    name=name,
                    host=host,
                    cause=opt_cause,
                    detail=opt_detail,
                    db_writer=db_writer,
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
                        name=name,
                        host=host,
                        db_writer=db_writer,
                    )
                    if env.exit_after:
                        stop.set()
                        return
                    if stop.is_set():
                        return
        except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad; supervisor decides retry vs terminate)
            last_exc = exc
            # Auth/credential death (expired or rotated OAuth token,
            # 401, invalid key) bubbles up here as a generic SDK
            # exception. Classify it so the operator sees a LOUD,
            # specific signal with the manual-refresh hint instead of an
            # ambiguous ``sdk-crash`` they only notice by the silence.
            from ._auth_failure import (
                AUTH_FAILURE_CAUSE,
                classify_auth_failure,
            )

            auth_detail = classify_auth_failure(exc)
            if auth_detail is not None:
                cause = AUTH_FAILURE_CAUSE
                detail = auth_detail
                error_kind = "auth_expired"
                logger.error(
                    "claude-session AUTH FAILURE for %s: %s", name, auth_detail
                )
            else:
                cause = "sdk-crash"
                detail = str(exc)
                error_kind = "sdk_runtime"
                logger.exception("claude-session conversation failed for %s", name)
            append_session_message(
                state_dir,
                {
                    "type": "error",
                    "kind": error_kind,
                    "detail": detail,
                    "attempt": attempt,
                },
            )
            if host:
                report_sdk_error(
                    name=name,
                    host=host,
                    cause=cause,
                    detail=detail,
                    db_writer=db_writer,
                )
            # Auth failures are terminal: retrying with the same expired
            # token only burns the backoff window and emits N identical
            # confusing crashes. Recovery is manual (`claude login`), so
            # stop now regardless of ``max_restarts``.
            if auth_detail is not None or attempt >= max_restarts or stop.is_set():
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
