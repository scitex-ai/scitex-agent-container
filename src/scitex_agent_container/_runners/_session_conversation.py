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

The per-turn driver (:func:`._session_turn._drive_turn`) is a sibling
module so neither file exceeds the per-file line cap. It is re-exported
here (along with :func:`._session_turn._safe_repr` and
:func:`._session_turn._build_completion_push_fn`) so existing imports
of those names from this module keep resolving.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ._rate_limit_reactive import handle_rate_limit_failure
from ._session_dead_recovery import handle_dead_session_resume
from ._session_state import (
    append_session_message,
    report_sdk_error,
)
from ._session_supervisor_helpers import (
    _drain_failed_inbox as _drain_failed_inbox,
)
from ._session_supervisor_helpers import (
    _maybe_compact as _maybe_compact,
)
from ._session_supervisor_helpers import (
    _resume_candidate as _resume_candidate,
)
from ._session_supervisor_helpers import (
    _wake_on_inbound as _wake_on_inbound,
)
from ._session_tasks import (
    TaskObservations,
    log_observability_status,
    resolve_task_types,
)
from ._session_turn import (
    _build_completion_push_fn,
    _drive_turn,
    _safe_repr,
)
from ._stderr_capture import (
    StderrCapture,
    enrich_detail_with_stderr,
    write_stderr_log,
)

logger = logging.getLogger(__name__)


async def run_conversation(
    name: str,
    state_dir: Path,
    *,
    pid: int,
    inbox: "asyncio.Queue",
    resume_session_id: str | None,
    stop: asyncio.Event,
    fork_session: bool = False,
    session_id: str | None = None,
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

    # Background-subagent observation (C2): one TaskObservations holder for
    # the whole conversation (NOT per-turn — completions must survive across
    # turns so the next turn / autonomous loop can read them). ``task_types``
    # maps the SDK's task-message classes to their session.jsonl event type;
    # it is empty when the installed SDK predates background-subagent
    # observation, in which case ``is_task_message`` never matches and the
    # receive loop is unchanged. The availability is logged once (LOUD at
    # WARNING when unavailable) so the gap is never a silent swallow.
    task_observations = TaskObservations()
    task_types = resolve_task_types(sdk_module)
    log_observability_status(task_types, name=name)

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
    # Dead-session recoveries are NOT charged to ``max_restarts``: a stale
    # resume marker is a recoverable, self-healing condition (discard the
    # dead id → fresh start), not a crash budget. Bound it separately so a
    # pathological loop (discard somehow fails to purge the id) still
    # terminates instead of spinning forever. One per distinct dead id is
    # the realistic ceiling — the latest + every forked id in the history.
    dead_session_recoveries = 0
    # Consecutive REACTIVE rate-limit (Mode A backoff) hits, back-to-back
    # with no intervening non-rate-limit failure or successful rotation.
    # Owned by ``handle_rate_limit_failure`` / ``_account.backoff_agent``
    # (task #13) — see ``_rate_limit_reactive`` module docstring.
    consecutive_rate_limit_hits = 0
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
        # Session FORK — the SDK peers of ``claude --fork-session`` /
        # ``--session-id``, threaded through the documented ``extra`` seam
        # (both are real ClaudeAgentOptions fields). Used by a twin's FIRST
        # boot to inherit its parent's conversation while writing to a session
        # of its own.
        #
        # FIRST ATTEMPT ONLY, and that guard is load-bearing: attempt>0 is the
        # supervisor's resume-RECOVERY walking ``session_id_history`` after a
        # rejected resume. Re-forking there would fight the recovery (each
        # retry forking a fresh thread), and re-pinning the SAME ``session_id``
        # would ask claude to create an id that attempt 0 may have already
        # created. Recovery must resume plainly.
        if attempt == 0:
            if fork_session:
                attempt_extra["fork_session"] = True
            if session_id:
                attempt_extra["session_id"] = session_id
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

        # Instrumentation (bug #42 hardening, 2026-06-07): name every
        # stdio MCP we are about to hand to the SDK so a future "MCP X
        # silently dropped" recurrence (operator-visible symptom: bot
        # stopped responding after restart) leaves a self-diagnosing
        # trail in stdout.log. The corresponding ``apptainer_restart``
        # mount + lock race that originally caused this is closed in
        # ``_lifecycle/_stop_escalate.py::ensure_previous_runtime_down``;
        # this log is the OBSERVABILITY half so a regression of either
        # the race or the SDK's per-MCP launch is visible without
        # bouncing the agent or attaching to its stderr.
        try:
            mcp_keys = sorted((getattr(options, "mcp_servers", None) or {}).keys())
        except Exception:  # stx-allow: fallback (reason: options surface is SDK-version-dependent — never fail the conversation on a logging probe)
            mcp_keys = ["<unreadable>"]
        logger.info(
            "claude-session %s: launching SDK (attempt=%d resume=%s mcp_servers=%r)",
            name,
            attempt,
            current_sid,
            mcp_keys,
        )
        try:
            async with ClaudeSDKClient(options=options) as client:
                while True:
                    env = await inbox.get()
                    if isinstance(env, ShutdownEnvelope):
                        return
                    if not isinstance(env, TurnEnvelope):
                        continue
                    # Wake-on-inbound (#41, lead a2a f39bdcc5 + b4e223e0):
                    # Spawn a concurrent task that watches the inbox while
                    # the SDK is driving the current turn. If a new envelope
                    # arrives MID-TURN (i.e. while ``_drive_turn``'s
                    # ``receive_response()`` iterator is awaiting on a long
                    # tool call — bash monitor, idle MCP), the wake task
                    # calls ``client.interrupt()`` so the SDK winds the
                    # current iterator down cleanly. ``_drive_turn`` then
                    # returns, this loop re-enters ``await inbox.get()``,
                    # and the queued envelope dequeues immediately. Without
                    # this, the agent sits at near-0% CPU until the SDK's
                    # idle tool returns on its own (the wedge mode that
                    # blocks operator + lead sends on proj-paper-scitex-clew
                    # and proj-neurovista, 2026-06-07).
                    #
                    # ``wait_for_item`` is non-destructive — the inbox
                    # entry stays in the queue and is dequeued by the
                    # next iteration's ``inbox.get()`` above. The wake
                    # task is always cancelled in the ``finally`` so it
                    # cannot leak across turn boundaries.
                    wake_task = asyncio.create_task(
                        _wake_on_inbound(inbox, client),
                        name=f"wake-on-inbound-{name}",
                    )
                    try:
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
                            task_observations=task_observations,
                            task_types=task_types,
                        )
                    finally:
                        wake_task.cancel()
                        try:
                            await wake_task
                        except (
                            asyncio.CancelledError,
                            BaseException,
                        ):  # stx-allow: fallback (reason: wake task is best-effort; never let its bookkeeping kill the conversation loop)
                            pass
                    # Proactive auto-compaction for provider-backed agents on a
                    # small context window (e.g. Qwen 128k via the LiteLLM
                    # shim). No-op unless SAC_AUTO_COMPACT_TOKENS is set, so the
                    # Anthropic / CLI native-compaction path stays unchanged.
                    # See _maybe_compact.
                    await _maybe_compact(client, name=name)
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
            # ``stderr_log_path`` becomes a stable on-disk pointer the lead
            # can read directly (``<state_dir>/runner-stderr.log``); ``None``
            # when nothing was captured. Both this path and the captured
            # text are splatted onto every error event below so the lead
            # has the real stderr without having to parse it back out of
            # the bounded ``detail`` string (sac-log-assistant-text
            # PARTIAL fix — closes the STDERR-capture gap).
            stderr_log_path = write_stderr_log(state_dir, captured_stderr)
            stderr_event_fields: dict[str, Any] = {}
            if captured_stderr:
                stderr_event_fields["stderr"] = captured_stderr
            if stderr_log_path is not None:
                stderr_event_fields["stderr_log"] = str(stderr_log_path)

            # Dead-session resume — the SDK rejected our --resume target.
            # Delegate the recoverable-vs-not classification, purge, and
            # event emission to ``_session_dead_recovery`` (extracted to
            # keep this file under the per-file line cap). Returns ``True``
            # when handled → reset the attempt counter and re-enter the
            # supervisor loop for a fresh start.
            if handle_dead_session_resume(
                enriched=enriched,
                state_dir=state_dir,
                name=name,
                host=host,
                attempt=attempt,
                current_sid=current_sid,
                stderr_event_fields=stderr_event_fields,
                append_session_message=append_session_message,
                report_sdk_error=report_sdk_error,
                db_writer=db_writer,
                stop_is_set=stop.is_set(),
                dead_session_recoveries=dead_session_recoveries,
            ):
                dead_session_recoveries += 1
                attempt = 0
                resume_session_id = None
                continue

            # Reactive rate-limit (task #13): detect + Mode A/B dispatch
            # delegated to ``_rate_limit_reactive``. ``handled=False`` ->
            # no rate-limit signal -> fall through unchanged below.
            rl_outcome = handle_rate_limit_failure(
                enriched=enriched,
                state_dir=state_dir,
                name=name,
                host=host,
                attempt=attempt,
                consecutive_hits=consecutive_rate_limit_hits,
                stderr_event_fields=stderr_event_fields,
                append_session_message=append_session_message,
                report_sdk_error=report_sdk_error,
                db_writer=db_writer,
            )
            consecutive_rate_limit_hits = rl_outcome.consecutive_hits
            if rl_outcome.handled and rl_outcome.reset_attempt:
                # Mode B rotated onto a fresh account: retry now, same
                # contract as a handled dead-session recovery.
                attempt = 0
                continue
            if rl_outcome.handled:
                # Mode A (or Mode B with nowhere to rotate to — "never
                # park", fall back to same-account backoff). Respects
                # max_restarts/stop; waits rl_outcome.delay_s instead of
                # the generic exponential backoff.
                if attempt >= max_restarts or stop.is_set():
                    _drain_failed_inbox(inbox, exc)
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=rl_outcome.delay_s)
                    _drain_failed_inbox(inbox, last_exc)
                    return
                except asyncio.TimeoutError:
                    pass
                attempt += 1
                continue

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
                    **stderr_event_fields,
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


# Re-export the per-turn driver + its helpers (moved to ._session_turn to
# keep both modules under the per-file line cap) so existing imports of
# these names from this module keep resolving (claude_session.py imports
# _safe_repr + _drain_failed_inbox + run_conversation from here).
__all__ = [
    "_build_completion_push_fn",
    "_drain_failed_inbox",
    "_drive_turn",
    "_resume_candidate",
    "_safe_repr",
    "run_conversation",
]
