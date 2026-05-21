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
import os
import sys
from pathlib import Path
from typing import Any, Optional

from ._session_state import (
    STATE_IDLE,
    STATE_WORKING,
    accumulate_quota,
    append_session_message,
    read_session_id,
    read_session_id_history,
    record_turn_transition,
    report_sdk_error,
    write_heartbeat,
    write_session_id,
)
from ._stderr_capture import (
    StderrCapture,
    enrich_detail_with_stderr,
    write_stderr_log,
)

logger = logging.getLogger(__name__)


def _safe_repr(value: object) -> str:
    """Bounded repr so a runaway tool-result blob can't bloat session.jsonl."""
    s = repr(value)
    return s if len(s) <= 1024 else s[:1024] + "…"  # stx-allow: STX-NL001


def _build_completion_push_fn(agent_name: str):
    """Build the Stop-hook completion ``push_fn``, or ``None`` if not wired.

    The push reuses the agent's own ``sac listen`` (``message:send``) — its
    base URL + bearer ride in the runner's env (``SAC_LISTEN_BASE_URL`` /
    ``SAC_LISTEN_BEARER``), injected by the apptainer runtime alongside the
    channel adapter. When no listen URL is configured (e.g. a bare runner
    with no host control-plane), there is nowhere to push a report, so we
    return ``None`` and the Stop hook keeps its append-only behaviour. This
    is the honest "no channel → no push" case, NOT a silenced failure: a
    push that has a URL but cannot deliver still fails LOUD inside the hook.
    """
    listen_url = os.environ.get("SAC_LISTEN_BASE_URL", "").strip()
    if not listen_url:
        return None
    bearer = os.environ.get("SAC_LISTEN_BEARER") or None

    from ._session_completion import push_completion

    async def _push_fn(
        report: dict, requester: str, dispatch_id: Optional[str]
    ) -> None:
        await push_completion(
            agent=agent_name,
            requester=requester,
            report=report,
            listen_url=listen_url,
            bearer=bearer,
            dispatch_id=dispatch_id,
        )

    return _push_fn


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


def _resume_candidate(
    state_dir: Path,
    *,
    attempt: int,
    fallback: str | None,
) -> str | None:
    """Pick the resume session_id for supervisor restart ``attempt``.

    Walks the append-only ``session_id_history`` from latest to oldest:
    ``attempt`` 0 → latest, 1 → next-older, etc. This lets a supervised
    restart retry a prior still-on-disk id when the latest one was
    forked or aged out and its resume is rejected.

    - ``attempt`` within the history range → that id (latest-first).
    - ``attempt == 0`` with no history yet → ``read_session_id`` if
      present else ``fallback`` (preserves the pre-history behaviour for
      the very first start, before any turn has recorded an id).
    - ``attempt`` beyond the history → ``None`` (history exhausted →
      fresh start, resume disabled).
    """
    history = read_session_id_history(state_dir)
    candidates = list(reversed(history))  # latest-first
    if attempt < len(candidates):
        return candidates[attempt]
    if attempt == 0:
        return read_session_id(state_dir) or fallback
    return None


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
    stderr_capture: Any | None = None,
    turn_context: Any | None = None,
    push_fn: Any | None = None,
) -> None:
    AssistantMessage = sdk_types["AssistantMessage"]
    TextBlock = sdk_types["TextBlock"]
    UserMessage = sdk_types["UserMessage"]
    ResultMessage = sdk_types["ResultMessage"]

    # Open the turn context BEFORE any work so the Stop hook (which fires
    # after the SDK drains this turn) can address the requester that
    # dispatched it. Requester identity rides on the envelope (threaded by
    # the inbound /v1/turn handler from the POST body — see _session_http).
    # ``None`` requester == a mission/boot turn with no peer to answer to.
    if turn_context is not None:
        turn_context.begin(
            requester=getattr(env, "from_agent", None),
            dispatch_id=getattr(env, "dispatch_id", None),
        )

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
                    # Fork detection: the SDK can return a NEW session id
                    # on resume instead of the one we asked it to resume
                    # (a fork). The latest marker still advances to the
                    # fork below — we don't change behaviour here — but a
                    # silent transition would orphan the prior id, so log
                    # it LOUDLY (warning) to make the fork observable. The
                    # prior id is preserved in session_id_history (see
                    # write_session_id).
                    prev_sid = read_session_id(state_dir)
                    if prev_sid and prev_sid != sid:
                        logger.warning(
                            "session_id changed on resume: %s -> %s "
                            "(SDK fork?); prior id retained in "
                            "session_id_history",
                            prev_sid,
                            sid,
                        )
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
                # Clean drain: record the HONEST success outcome + reply
                # summary on the turn context BEFORE the Stop hook fires
                # (Stop comes after this ResultMessage). The Stop hook reads
                # this to PUSH the completion report to the requester.
                if turn_context is not None:
                    from ._session_completion import STATUS_SUCCESS

                    turn_context.finish(status=STATUS_SUCCESS, summary="".join(chunks))
                break
    except BaseException as exc:  # stx-allow: fallback (reason: enrich the failure with the real captured stderr, resolve the awaiter with the real cause, then re-raise for the supervisor)
        # The SDK turn failed (e.g. a ProcessError from a stale --resume
        # whose only "stderr" is the placeholder). Fold the stderr the
        # registered callback captured into the exception so the awaiting
        # /v1/turn handler returns a 502 carrying the REAL cause instead
        # of a silent empty-200 (the pre-fix behaviour: the finally below
        # resolved the future with "".join(chunks)). Then re-raise so
        # run_conversation's supervisor records + classifies it.
        if not env.response.done():
            captured = stderr_capture.text() if stderr_capture is not None else ""
            enriched = enrich_detail_with_stderr(str(exc), captured)
            if enriched != str(exc):
                # Attach the enriched detail without losing the exception
                # type, so classify_auth_failure / the supervisor still
                # see the original class.
                exc.args = (enriched,) + tuple(exc.args[1:])
            env.response.set_exception(exc)
        # Record the HONEST failure outcome on the turn context. Stop does
        # NOT reliably fire when the SDK raises mid-turn, so the finally
        # below is what emits the requester push in this case (status from
        # here). Summary carries the failure detail so the requester sees
        # WHY, not a bare "failure".
        if turn_context is not None:
            from ._session_completion import STATUS_FAILURE

            turn_context.finish(status=STATUS_FAILURE, summary=str(exc))
        raise
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
        # Emit the requester completion push exactly once per turn. On a
        # clean drain the Stop hook already pushed (pushed=True) → this is a
        # no-op; on an SDK error (no clean Stop) THIS is the emit point,
        # carrying the honest FAILURE status set in the except branch. The
        # ``pushed`` guard inside ``emit_completion_push`` makes the double
        # call idempotent. A no-requester (mission) turn skips quietly.
        if turn_context is not None and push_fn is not None and name:
            from ._session_hooks import emit_completion_push

            await emit_completion_push(turn_context, push_fn, agent_name=name)
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
    from ._session_hooks import TurnContext, build_event_log_hooks

    if build_sdk_options_fn is None:
        build_sdk_options_fn = build_sdk_options

    # Requester-feedback wiring: one TurnContext shared across the whole
    # conversation (turns are serial, so a single holder is race-free) plus
    # a completion push_fn resolved from the runner's listen env. The Stop
    # hook reads the context to PUSH a completion report back to whoever
    # dispatched each turn. ``push_fn`` is ``None`` when no host control-
    # plane is configured (bare runner) — then the Stop hook keeps its
    # append-only behaviour and ``_drive_turn`` skips the finally emit.
    turn_context = TurnContext()
    push_fn = _build_completion_push_fn(name)
    hooks = build_event_log_hooks(
        name, HookMatcher, turn_context=turn_context, push_fn=push_fn
    )

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
        # Resume target, with history fallback. On the first attempt this
        # is the latest completed-turn id (== read_session_id, since
        # write_session_id appends to the history). On each supervised
        # restart we step to a progressively OLDER id from the
        # append-only history before giving up on resume — so if the
        # latest id was forked/aged-out and its resume is rejected, a
        # prior still-on-disk id gets a chance rather than the runner
        # losing the whole conversation. Once the history is exhausted we
        # fall through to a fresh start (resume=None).
        current_sid = _resume_candidate(
            state_dir, attempt=attempt, fallback=resume_session_id
        )
        if attempt > 0:
            logger.warning(
                "claude-session resume retry %d for %s: trying session_id %s "
                "(walking session_id_history)",
                attempt,
                name,
                current_sid,
            )
        # Fresh per-attempt stderr collector. The SDK only PIPES the
        # claude subprocess's stderr when a ``stderr`` callback is
        # registered on ``ClaudeAgentOptions``; without it a ProcessError
        # carries only the useless placeholder "Check stderr output for
        # details". Register the callback through the ``extra`` seam
        # (``stderr`` is a real ClaudeAgentOptions field) so the real
        # failure reason — e.g. "No conversation found for session <id>"
        # on a stale --resume — reaches both the persisted error record
        # and the awaiting /v1/turn future.
        stderr_capture = StderrCapture()
        attempt_extra = dict(sdk_extra) if sdk_extra else {}
        attempt_extra["stderr"] = stderr_capture.callback
        try:
            options = build_sdk_options_fn(
                name,
                permission_mode="bypassPermissions",
                resume=current_sid,
                hooks=hooks,
                extra=attempt_extra,
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
                        stderr_capture=stderr_capture,
                        turn_context=turn_context,
                        push_fn=push_fn,
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

            # Fold the captured subprocess stderr into the failure string
            # so BOTH auth classification AND the persisted detail see the
            # real reason — not the SDK's "Check stderr output for details"
            # placeholder. Also persist the full stream to a dedicated log
            # so the actionable tail survives even when ``detail`` is
            # bounded downstream. (When the failure came from _drive_turn,
            # str(exc) is already enriched; enrich_detail_with_stderr is a
            # no-op there since the captured text is already a substring.)
            captured_stderr = stderr_capture.text()
            enriched = enrich_detail_with_stderr(str(exc), captured_stderr)
            write_stderr_log(state_dir, captured_stderr)

            auth_detail = classify_auth_failure(enriched)
            if auth_detail is not None:
                cause = AUTH_FAILURE_CAUSE
                detail = auth_detail
                error_kind = "auth_expired"
                logger.error(
                    "claude-session AUTH FAILURE for %s: %s", name, auth_detail
                )
            else:
                cause = "sdk-crash"
                detail = enriched
                error_kind = "sdk_runtime"
                logger.exception(
                    "claude-session conversation failed for %s: %s", name, detail
                )
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
