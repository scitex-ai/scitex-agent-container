"""The codex-family turn driver for the shared session daemon.

Card ``sac-codex-python-sdk-harness-20260814``. Same division of labour
the openai driver got in v4 step 7: the daemon (:mod:`.session_daemon`)
owns the PROCESS — pid file, signals, heartbeat side-task, residency
parking, the a2a sidecar, exit accounting — and this module owns only
the TURN. Its single public callable, :func:`run_codex_conversation`,
satisfies the daemon's turn-driver contract (see the
:mod:`.session_daemon` module docstring for the call shape).

WHAT THE DRIVER MAY TOUCH: the inbox, the per-turn transcript, the quota
totals, the BUSY/READY beats it testifies to as
:data:`~._incarnation.WRITER_TURN_DRIVER`, and the vendor session
(:class:`~.codex_session.CodexSession`). WHAT IT MUST NOT TOUCH: the pid
file, the periodic heartbeat loop, the a2a sidecar, ``exit.json``.

Registry contract (``config._harness_registry``): the ``codex-sdk``
descriptor declares ``hosted="runner"`` (this daemon hosts it),
``beat_writer="in-process"`` (the beats below), and — unlike every other
runner-hosted entry — ``can_resume=True``. So ``resume_session_id`` is
HONOURED here rather than refused: it is passed to the SDK's
``thread_resume`` as the codex thread id. The gate still READS the
descriptor rather than assuming, so flipping the field is the only edit
needed to change the behaviour.

The per-turn event pump is shared with the openai driver
(:func:`._harness_turn_pump.drive_harness_turn`) — the bookkeeping is a
function of :class:`~._harness_session.NormalizedEvent` kinds and owes
nothing to any vendor.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config._harness_registry import CODEX_SDK, HARNESS_DESCRIPTORS
from ._harness_session import Message
from ._harness_turn_pump import drive_harness_turn
from ._session_state import append_session_message, report_sdk_error
from ._session_supervisor_helpers import _drain_failed_inbox

logger = logging.getLogger(__name__)

__all__ = ["run_codex_conversation"]


def _default_session_factory() -> Any:
    """The production session class, imported call-time.

    Lazy so importing THIS module never pulls the (optional)
    ``openai-codex`` dependency chain — which includes a 285 MB native
    binary wheel — and so the import order between :mod:`.codex_session`
    and :mod:`._codex_session_cli` stays acyclic.
    """
    from .codex_session import CodexSession

    return CodexSession


async def run_codex_conversation(
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
    """Drive an inbox-fed conversation against one :class:`CodexSession`.

    The daemon's turn-driver seam for the ``codex-sdk`` harness: drains
    :class:`~._session_inbox.TurnEnvelope` items until a
    :class:`~._session_inbox.ShutdownEnvelope` (or an ``exit_after``
    turn — the one-shot handshake) ends the run. ONE vendor session is
    held open for the whole conversation, which for this harness means
    one ``codex app-server`` subprocess and one thread; multi-turn
    memory is the thread's, kept by codex under ``$CODEX_HOME``.

    ``resume_session_id`` is the CODEX THREAD ID and is honoured (the
    descriptor declares ``can_resume=True``): the session resumes that
    thread instead of starting a fresh one. A resume the SDK rejects
    fails the session OPEN, loudly — never a silent fresh start under a
    caller-supplied id, which would promise a continuity that does not
    exist.

    ``max_restarts`` / ``restart_backoff_s`` are accepted per the
    turn-driver contract but unused: the app-server subprocess is owned
    by the SDK, and a mid-session crash surfaces as a turn-ending error
    event rather than something this driver can respawn around.
    ``channels`` is the agent-level ``spec.comms.channels`` declaration.  Its
    source-specific adapters run upstream of the harness and normalize every
    admitted message into this daemon's shared inbox.  The Codex driver must
    therefore neither reinterpret nor reject the declaration: native Codex
    delivery begins at the inbox boundary (``turn/start`` while idle,
    ``turn/steer`` while a turn is active).
    """
    from ._session_inbox import ShutdownEnvelope, TurnEnvelope

    del max_restarts, restart_backoff_s, a2a_port, channels  # see docstring

    descriptor = HARNESS_DESCRIPTORS[CODEX_SDK]
    if resume_session_id and not descriptor.can_resume:
        # Defensive, and deliberately NOT dead code: it reads the
        # descriptor rather than a constant, so flipping can_resume to
        # False in the registry makes the refusal real without touching
        # this module. The openai driver's refusal has the same shape.
        detail = (
            f"harness {CODEX_SDK!r} declares can_resume=False in the "
            f"harness registry; refusing resume_session_id="
            f"{resume_session_id!r} for agent {name!r}."
        )
        logger.error("%s", detail)
        _drain_failed_inbox(inbox, RuntimeError(detail))
        return

    factory = session_factory if session_factory is not None else _default_session_factory()
    try:
        session = factory(name, thread_id=resume_session_id)
        await session.start()
    except Exception as exc:  # stx-allow: fallback (reason: init failure is terminal — record + drain so producers don't hang, mirroring the openai driver's session-open path; the daemon accounts the early return)
        logger.error("codex session failed to open for %s: %s", name, exc)
        append_session_message(
            state_dir, {"type": "error", "kind": "session_open", "detail": str(exc)}
        )
        if host:
            report_sdk_error(name=name, host=host, cause="session-open", detail=str(exc))
        _drain_failed_inbox(inbox, exc)
        return

    pending_env: Any | None = None
    try:
        while True:
            env = pending_env if pending_env is not None else await inbox.get()
            pending_env = None
            if isinstance(env, ShutdownEnvelope):
                return
            if not isinstance(env, TurnEnvelope):
                continue
            turn_task = asyncio.create_task(
                drive_harness_turn(
                    session,
                    env,
                    state_dir=state_dir,
                    pid=pid,
                    stop=stop,
                    print_stream=print_stream,
                    name=name,
                    host=host,
                    harness=CODEX_SDK,
                )
            )
            steered_envs: list[Any] = []

            # The old SDK runner waited for ``thread.run`` to finish before
            # looking at the inbox again.  When both surfaces are available,
            # race the native Codex turn against the non-destructive inbox
            # event and route arriving messages through app-server's
            # ``turn/steer`` method.  Plain asyncio.Queue fixtures and older
            # session implementations retain the serial behavior.
            can_wake = callable(getattr(inbox, "wait_for_item", None))
            can_steer = callable(getattr(session, "steer", None))
            while can_wake and can_steer and not turn_task.done():
                wake_task = asyncio.create_task(inbox.wait_for_item())
                done, _ = await asyncio.wait(
                    {turn_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if turn_task in done:
                    wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
                    break

                incoming = await inbox.get()
                if isinstance(incoming, ShutdownEnvelope):
                    interrupt = getattr(session, "interrupt", None)
                    active_id = getattr(session, "active_turn_id", None)
                    if callable(interrupt) and active_id:
                        await interrupt(expected_turn_id=active_id)
                    await turn_task
                    return
                if not isinstance(incoming, TurnEnvelope):
                    continue

                active_id = getattr(session, "active_turn_id", None)
                if not active_id:
                    # The turn completed between the wake and the precondition
                    # read. Preserve FIFO by making this the next normal turn.
                    pending_env = incoming
                    break
                try:
                    acknowledged_id = await session.steer(
                        Message(role="user", content=incoming.text),
                        expected_turn_id=active_id,
                    )
                except Exception as exc:
                    # Once the RPC was attempted, its outcome is authoritative:
                    # fail this delivery loudly. Retrying it as a fresh turn
                    # would change steer semantics and could duplicate input if
                    # the acknowledgement alone was lost.
                    append_session_message(
                        state_dir,
                        {
                            "type": "error",
                            "kind": "native-steer",
                            "detail": str(exc),
                            "message_id": incoming.dispatch_id,
                        },
                    )
                    if not incoming.response.done():
                        incoming.response.set_exception(exc)
                    continue
                append_session_message(
                    state_dir,
                    {
                        "type": "user",
                        "text": incoming.text,
                        "delivery": "native-steer",
                        "codex_turn_id": acknowledged_id,
                        "message_id": incoming.dispatch_id,
                    },
                )
                steered_envs.append(incoming)

            try:
                await turn_task
            except BaseException as exc:
                # Every accepted native steer is attached to this turn. If
                # the turn itself crashes, resolve those callers with the
                # same failure instead of leaving their HTTP futures parked.
                for steered in steered_envs:
                    if not steered.response.done():
                        if isinstance(exc, asyncio.CancelledError):
                            steered.response.cancel()
                        else:
                            steered.response.set_exception(exc)
                raise
            # Codex produces one terminal assistant result for the active turn,
            # including all accepted steering input. Every synchronous caller
            # attached to that turn receives the same terminal result and
            # resume id; the transcript above preserves which input was
            # delivered through native steer.
            for steered in steered_envs:
                steered.session_id = env.session_id
                if not steered.response.done():
                    if env.response.cancelled():
                        steered.response.cancel()
                    else:
                        failure = env.response.exception()
                        if failure is not None:
                            steered.response.set_exception(failure)
                        else:
                            steered.response.set_result(env.response.result())
            if env.exit_after:
                stop.set()
                return
            if stop.is_set():
                return
    finally:
        try:
            await session.close()
        except Exception as exc:  # stx-allow: fallback (reason: teardown must not mask the conversation's own outcome; a failed close here leaks a codex app-server subprocess, so it is logged loudly rather than raised over the real result)
            logger.error("codex session close failed for %s: %s", name, exc)
