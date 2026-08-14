"""The OpenAI-family turn driver for the shared session daemon.

v4 migration step 7 (card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``): the
``openai-agents`` runner no longer owns any process machinery. The
daemon (:mod:`.session_daemon`) owns the PROCESS — pid file, signals,
heartbeat side-task, residency parking, the a2a sidecar, exit
accounting; this module owns only the TURN, exactly like
:mod:`._session_conversation` does for the Claude harness. Its single
public callable, :func:`run_openai_conversation`, satisfies the
daemon's turn-driver contract (see the :mod:`.session_daemon` module
docstring for the call shape).

WHAT THE DRIVER MAY TOUCH: the inbox (drain envelopes), the per-turn
transcript (``session.jsonl`` via ``append_session_message``), the
quota totals (``accumulate_quota``), the BUSY/READY beats it testifies
to as :data:`~._incarnation.WRITER_TURN_DRIVER`, and the vendor session
(:class:`~.openai_session.OpenAIAgentsSession`). WHAT IT MUST NOT
TOUCH: the pid file, the periodic heartbeat loop, the a2a sidecar,
``exit.json`` — those are the daemon's, and duplicating them here is
the drift step 7 exists to delete.

Registry contract (v4 step 4, ``config._harness_registry``): the
``openai-agents`` descriptor declares ``hosted="runner"`` (this daemon
hosts it), ``beat_writer="in-process"`` (the beats below), and
``can_resume=False`` — so a ``resume_session_id`` is REFUSED loudly
here (and earlier, in :mod:`._openai_session_cli`), never silently
remapped. The ``SQLiteSession`` state db does persist turns across
process lifetimes under the agent's own name, but sac's resume
contract — rehydrate a PRIOR incarnation's conversation from a
caller-supplied session id — is not implemented for this harness;
honouring the flag would promise continuity the transcript format
cannot prove.

API surface (#1035, ``_openai_api_surface``): before the first turn the
driver points the SDK at ``chat_completions`` when ``OPENAI_BASE_URL``
names a self-hosted gateway — the fleet's gpt-oss/qwen endpoints route
``/v1/chat/completions`` only, and the SDK's Responses default turns a
healthy endpoint into an opaque 404.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from ..config._harness_registry import HARNESS_DESCRIPTORS, OPENAI_AGENTS
from ._harness_session import Message
from ._incarnation import WRITER_TURN_DRIVER
from ._openai_api_surface import select_api_surface
from ._session_state import (
    STATE_BUSY,
    STATE_READY,
    accumulate_quota,
    append_session_message,
    report_sdk_error,
    write_heartbeat,
)
from ._session_supervisor_helpers import _drain_failed_inbox
from ._session_turn import _safe_repr

logger = logging.getLogger(__name__)

__all__ = ["run_openai_conversation"]


def _default_session_factory() -> Any:
    """The production session class, imported call-time.

    Lazy so importing THIS module never pulls the (optional)
    ``openai-agents`` dependency chain, and so the module import order
    between :mod:`.openai_session` and :mod:`._openai_session_cli`
    stays acyclic.
    """
    from .openai_session import OpenAIAgentsSession

    return OpenAIAgentsSession


def _select_api_surface_if_possible() -> None:
    """Apply the #1035 chat-completions fix when the SDK is importable.

    A missing SDK is not swallowed here — ``session.start()`` raises the
    actionable install hint moments later; this helper only avoids
    raising a SECOND, less specific error first.
    """
    try:
        import agents as agents_mod
    except Exception:  # stx-allow: fallback (reason: optional dep — start() raises the actionable install hint; selecting a surface on an absent SDK is meaningless)
        return
    select_api_surface(agents_mod)


async def _drive_openai_turn(
    session: Any,
    env: Any,
    *,
    state_dir: Path,
    pid: int,
    stop: asyncio.Event,
    print_stream: bool,
    name: str,
    host: str | None,
) -> None:
    """Run ONE turn against the vendor session, resolving ``env.response``.

    Mirrors :func:`._session_turn._drive_turn`'s bookkeeping: a BUSY
    beat stamped :data:`WRITER_TURN_DRIVER` (self-testimony the daemon's
    periodic loop preserves), transcript appends per event, quota
    accumulation on the terminal result, and a READY beat once the turn
    closes. An ``error`` event is turn-ending per the HarnessSession
    contract: the awaiting future resolves with the failure instead of a
    silent empty reply. An EXCEPTION out of ``session.send`` is outside
    that contract — it propagates to the daemon, whose done-callback
    records ``crashed`` honestly.
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
                logger.error("openai turn failed for %s: %s", name, error_detail)
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


async def run_openai_conversation(
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
    host: str | None = None,
    channels: list[str] | None = None,
    a2a_port: int | None = None,
    session_factory: Any | None = None,
) -> None:
    """Drive an inbox-fed conversation against one ``OpenAIAgentsSession``.

    The daemon's turn-driver seam for the ``openai-agents`` harness:
    drains :class:`~._session_inbox.TurnEnvelope` items until a
    :class:`~._session_inbox.ShutdownEnvelope` (or an ``exit_after``
    turn — the one-shot handshake) ends the run. One vendor session is
    held open for the whole conversation; its ``SQLiteSession`` state db
    (keyed by the agent's name) carries multi-turn memory.

    ``max_restarts`` / ``restart_backoff_s`` are accepted per the
    turn-driver contract but intentionally unused: unlike the Claude
    SDK's long-lived subprocess there is no persistent vendor client to
    supervise — every turn is an independent chat-completions HTTP
    exchange, so a mid-session "client crash" has no equivalent here.
    ``channels`` likewise names Claude-SDK channel adapters that this
    harness does not implement; a spec that asks for them gets a LOUD
    warning, not a silent degradation.

    ``session_factory`` is the test seam (defaults to
    :class:`~.openai_session.OpenAIAgentsSession`); it is called as
    ``factory(name, mcp_servers=..., **{})`` and must return an object
    with the HarnessSession ``start`` / ``send`` / ``close`` surface.
    """
    from ._session_inbox import ShutdownEnvelope, TurnEnvelope

    del max_restarts, restart_backoff_s, a2a_port  # contract params; see docstring

    descriptor = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    if resume_session_id and not descriptor.can_resume:
        detail = (
            f"harness {OPENAI_AGENTS!r} declares can_resume=False in the "
            f"harness registry; refusing resume_session_id="
            f"{resume_session_id!r} for agent {name!r} rather than "
            "silently starting a conversation that does not continue the "
            "one the caller named."
        )
        logger.error("%s", detail)
        append_session_message(
            state_dir, {"type": "error", "kind": "resume_refused", "detail": detail}
        )
        if host:
            report_sdk_error(name=name, host=host, cause="resume-refused", detail=detail)
        _drain_failed_inbox(inbox, RuntimeError(detail))
        return

    if channels:
        logger.warning(
            "openai harness has no channel adapters; --channels %r ignored "
            "for agent %s (channel wiring is Claude-SDK-specific)",
            channels,
            name,
        )

    _select_api_surface_if_possible()

    from ..runtimes._openai_sdk_common import resolve_agent_workspace

    factory = session_factory if session_factory is not None else _default_session_factory()
    mcp_servers, _workspace_cwd = resolve_agent_workspace(name)
    try:
        session = factory(name, mcp_servers=mcp_servers)
        await session.start()
    except Exception as exc:  # stx-allow: fallback (reason: init failure is terminal — record + drain so producers don't hang, mirroring the Claude driver's sdk-missing/options path; the daemon accounts the early return)
        logger.error("openai session failed to open for %s: %s", name, exc)
        append_session_message(
            state_dir, {"type": "error", "kind": "session_open", "detail": str(exc)}
        )
        if host:
            report_sdk_error(
                name=name, host=host, cause="session-open", detail=str(exc)
            )
        _drain_failed_inbox(inbox, exc)
        return

    try:
        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if not isinstance(env, TurnEnvelope):
                continue
            await _drive_openai_turn(
                session,
                env,
                state_dir=state_dir,
                pid=pid,
                stop=stop,
                print_stream=print_stream,
                name=name,
                host=host,
            )
            if env.exit_after:
                stop.set()
                return
            if stop.is_set():
                return
    finally:
        try:
            await session.close()
        except Exception as exc:  # stx-allow: fallback (reason: teardown must not mask the conversation's own outcome; the failure is still logged loudly)
            logger.error("openai session close failed for %s: %s", name, exc)
