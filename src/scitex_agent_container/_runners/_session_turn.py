"""The per-turn SDK driver for the claude-session runner.

Split out of :mod:`._session_conversation` so each module stays under
the project's per-file line cap. :func:`_drive_turn` drains one turn's
SDK response stream — assistant text, the user echo, the terminal
``ResultMessage``, and (autonomy C2) the background-subagent
task-lifecycle messages — persisting each to ``session.jsonl`` and
firing the requester completion push. The supervisor / inbox loop that
calls it lives in :mod:`._session_conversation`.

Background-subagent observation (C2)
------------------------------------
A background subagent (``task_type == "local_agent"``) interleaves
``TaskStartedMessage`` / ``TaskProgressMessage`` /
``TaskNotificationMessage`` into the turn's response stream. The loop
used to drop them, so a background subagent's result reached *nobody*.
:func:`_drive_turn` now captures each into ``session.jsonl`` AND files
it on the conversation-lifetime :class:`._session_tasks.TaskObservations`
holder so the NEXT turn / the autonomous loop (C3, a separate item) can
read which background subagents completed and what they produced. Task
messages do NOT break the turn — only ``ResultMessage`` does.
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
    record_turn_transition,
    write_heartbeat,
    write_session_id,
)
from ._session_tasks import handle_task_message, is_task_message
from ._stderr_capture import enrich_detail_with_stderr

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
    task_observations: Any | None = None,
    task_types: dict | None = None,
) -> None:
    AssistantMessage = sdk_types["AssistantMessage"]
    TextBlock = sdk_types["TextBlock"]
    UserMessage = sdk_types["UserMessage"]
    ResultMessage = sdk_types["ResultMessage"]
    task_types = task_types or {}

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
            elif is_task_message(msg, task_types):
                # C2 — a background subagent's lifecycle signal interleaves
                # with the assistant stream. Capture it into session.jsonl
                # AND accumulate it on the conversation-lifetime observations
                # holder so the NEXT turn / autonomous loop can react to the
                # completion. Crucially: do NOT break — the turn continues.
                handle_task_message(
                    msg,
                    task_types[type(msg)],
                    observations=task_observations,
                    append_fn=append_session_message,
                    state_dir=state_dir,
                )
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
                cost_usd = getattr(msg, "total_cost_usd", None)
                model_usage = getattr(msg, "model_usage", None)
                accumulate_quota(state_dir, usage, cost_usd=cost_usd)
                append_session_message(
                    state_dir,
                    {
                        "type": "result",
                        "session_id": sid,
                        "usage": usage,
                        "cost_usd": cost_usd,
                        "model_usage": model_usage,
                    },
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


__all__ = ["_build_completion_push_fn", "_drive_turn", "_safe_repr"]
