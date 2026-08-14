"""Shared residency daemon for long-lived agent runners.

Extracted verbatim from :mod:`.claude_session` (v4 migration step 3 —
"sac owns the process; a harness owns only the turn"). This module owns
the RESIDENCY machinery: PID file write, SIGTERM/SIGINT handling, the
heartbeat side-task, the inbox that feeds turns to the harness, the
optional A2A inbound sidecar, the autonomous drive-until-done loop
(:mod:`._session_autonomous`), and clean shutdown. It knows nothing
about any vendor SDK — the actual conversation is delegated to the
``turn_driver`` callable that the harness-specific runner supplies
(:mod:`.claude_session` passes its
``_session_conversation.run_conversation``).

Turn-driver contract (the seam a future harness runner implements)::

    await turn_driver(
        name, state_dir,
        pid=..., inbox=..., resume_session_id=..., stop=...,
        print_stream=..., max_restarts=..., restart_backoff_s=...,
        host=..., channels=..., a2a_port=...,
    )

The driver drains :class:`._session_inbox.TurnEnvelope` items from
``inbox`` until it sees a :class:`._session_inbox.ShutdownEnvelope`,
resolving each envelope's ``response`` future with the assistant reply.

v4 step 5 — the liveness artifact + the residency contract
----------------------------------------------------------
The daemon's beats carry ``{incarnation_id, pid, seq, state,
turns_completed, ts, writer}`` with the resident state split into
``starting | busy | ready | stopping`` — READY meaning "the inbox has a
live consumer", which is precisely what a bare process-alive signal
could not say. On EVERY exit path the daemon writes a terminal
ExitRecord (``exit.json`` — see :mod:`._incarnation`) naming WHY it
ended: ``stopped-by-signal`` / ``oneshot-complete`` /
``harness-returned`` / ``crashed``.

"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any

from ._daemon_contract import (
    make_daemon_state_fn,
    resolve_exit,
)
from ._incarnation import (
    EXIT_ONESHOT_COMPLETE,
    EXIT_STOPPED_BY_SIGNAL,
    WRITER_SESSION_DAEMON,
    ExitReasonHolder,
    clear_exit_record,
    clear_incarnation_binding,
    try_bind_incarnation,
    write_exit_record,
)
from ._session_autonomous import _autonomous_loop
from ._session_state import (
    DEFAULT_TICK_SECONDS,
    STATE_STARTING,
    STATE_STOPPING,
    read_started_at,
    state_dir_for,
    write_heartbeat,
    write_pid,
    write_started_at,
)
from ._session_state import (
    heartbeat_loop as _heartbeat_loop,
)

logger = logging.getLogger(__name__)

__all__ = ["_autonomous_loop", "run_session_daemon"]


async def run_session_daemon(
    name: str,
    *,
    turn_driver: Any,
    state_root: Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    mission: str | None = None,
    resume_session_id: str | None = None,
    print_stream: bool = False,
    a2a_host: str = "127.0.0.1",
    a2a_port: int | None = None,
    a2a_card_yaml: str = "",
    channels: list[str] | None = None,
    autonomous_enabled: bool = False,
    autonomous_drive_until: str = "DONE",
    autonomous_max_turns: int = 50,
    autonomous_kick_text: str = "Continue. Print DONE when finished.",
    max_restarts: int = 0,
    restart_backoff_s: float = 1.0,
    serve_inbound_fn: Any | None = None,
    shutdown_timeout_s: float = 5.0,
) -> int:
    """Run the daemon loop until SIGTERM / SIGINT — or until the
    conversation task ends on its own (the residency contract; see the
    module docstring).

    Returns the exit code: 0 for a planned end (signal stop, one-shot
    completion), non-zero when the harness violated residency
    (``harness-returned`` / ``crashed``). Idempotent re-entry is *not*
    attempted — the adapter is responsible for ensuring at most one
    runner per name.

    ``turn_driver`` is the harness seam: the async callable that owns
    the conversation (see the module docstring for its call shape). The
    daemon spawns it as the conversation task whenever the inbox has a
    producer, and never touches the vendor SDK itself.

    If ``mission`` is given, the daemon drives one conversation turn
    against it; afterward it idles awaiting SIGTERM. With no mission,
    the daemon just heartbeats — useful for lifecycle correctness
    checks and for hand-driven manual sessions.
    """
    state_dir = state_dir_for(name, state_root)
    state_dir.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    # Resolve canonical host once so every diary write (heartbeat /
    # turn / error) tags the same hostname. _resolve_host falls back
    # to hostname -s when config.yaml is malformed, so this never
    # raises.
    from .._state.state_db import _resolve_host

    host = _resolve_host(None)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    # v4 step 5 — incarnation + exit bookkeeping. A NEW daemon must not
    # inherit its predecessor's incarnation bind (in-process test
    # harnesses run several daemons per interpreter) nor its farewell
    # ExitRecord; the bind is then re-attempted per beat until the start
    # path publishes the fresh ``instance_id`` marker.
    clear_incarnation_binding(state_dir)
    clear_exit_record(state_dir)
    try_bind_incarnation(state_dir)
    exit_cause = ExitReasonHolder()
    # ``convo_task`` is created further down; the beat state machine and
    # the exit resolution see it through this mutable ref.
    convo_ref: dict[str, asyncio.Task | None] = {"task": None}
    # A ``--print-stream`` mission is a PLANNED one-shot: the driver
    # returning is its success, not a residency violation.
    oneshot_mode = bool(mission and print_stream and not autonomous_enabled)

    def _on_signal(signum: int) -> None:
        logger.info("runner %s received signal %d, stopping", name, signum)
        exit_cause.set_once(EXIT_STOPPED_BY_SIGNAL, 0)
        write_heartbeat(
            state_dir,
            pid=pid,
            state=STATE_STOPPING,
            name=name,
            host=host,
            writer=WRITER_SESSION_DAEMON,
        )
        stop.set()

    # Register signal handlers BEFORE the first heartbeat write. The
    # initial write_heartbeat now also runs init_schema on state.db
    # (diary tables); on a fresh DB this can take long enough that a
    # racing SIGTERM from a fast test fixture would kill the process
    # before handlers were installed, producing exit -15 instead of 0.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (
            NotImplementedError
        ):  # stx-allow: fallback (reason: Windows / no asyncio signal support)
            signal.signal(sig, lambda s, _f: _on_signal(s))

    write_pid(state_dir, pid)
    # Record the session start time so every heartbeat can report
    # ``elapsed_s``. Preserve an existing value across a respawn that
    # resumes the same SDK session (resume_session_id present) — the
    # elapsed clock should track the conversation, not the process.
    if resume_session_id and read_started_at(state_dir) is not None:
        pass
    else:
        write_started_at(state_dir)
    write_heartbeat(
        state_dir,
        pid=pid,
        state=STATE_STARTING,
        name=name,
        host=host,
        writer=WRITER_SESSION_DAEMON,
    )

    hb_task = asyncio.create_task(
        _heartbeat_loop(
            state_dir,
            pid=pid,
            tick_seconds=tick_seconds,
            stop=stop,
            name=name,
            host=host,
            state_fn=make_daemon_state_fn(state_dir, stop=stop, convo_ref=convo_ref),
            writer=WRITER_SESSION_DAEMON,
        ),
    )

    from ._session_inbox import ShutdownEnvelope, TurnEnvelope, make_inbox

    inbox: asyncio.Queue = make_inbox()
    convo_task: asyncio.Task | None = None
    http_task: asyncio.Task | None = None

    def _write_exit() -> int:
        """Write the terminal ExitRecord; returns the exit code."""
        reason, code = resolve_exit(exit_cause, convo_ref)
        write_exit_record(
            state_dir,
            reason=reason,
            code=code,
            incarnation_id=try_bind_incarnation(state_dir),
            pid=pid,
        )
        return code

    if a2a_port is not None:
        if serve_inbound_fn is None:
            from ._session_http import (
                serve_inbound as serve_inbound_fn,  # type: ignore[no-redef]
            )

        http_task = asyncio.create_task(
            serve_inbound_fn(
                inbox,
                host=a2a_host,
                port=a2a_port,
                stop=stop,
                agent_name=name,
                spec_yaml_path=a2a_card_yaml,
                state_dir=state_dir,
            ),
        )

    # Spawn the conversation (turn-driver) task whenever the inbox has a
    # producer: mission seeds it with the boot prompt, or a2a_port lets
    # HTTP feed turns. Without a producer, no harness client is needed.
    autonomous_task: asyncio.Task | None = None
    if mission or a2a_port is not None:
        if mission and not autonomous_enabled:
            # Seed the inbox with the mission turn. exit_after=True only
            # for foreground (--print-stream) mode so the runner exits
            # when done.
            mission_env = TurnEnvelope(
                text=mission,
                response=loop.create_future(),
                exit_after=print_stream,
            )
            await inbox.put(mission_env)
        convo_task = asyncio.create_task(
            turn_driver(
                name,
                state_dir,
                pid=pid,
                inbox=inbox,
                resume_session_id=resume_session_id,
                stop=stop,
                print_stream=print_stream,
                max_restarts=max_restarts,
                restart_backoff_s=restart_backoff_s,
                host=host,
                channels=channels,
                a2a_port=a2a_port,
            )
        )
        convo_ref["task"] = convo_task
        if mission and autonomous_enabled:
            # F-CS3 phase 2: drive turns until drive_until matches or
            # max_turns is reached. The loop sets ``stop`` itself, so
            # the surrounding shutdown path handles cleanup uniformly.
            # The wrapper records the one-shot cause — UNLESS the driver
            # died mid-loop, in which case the done-callback's CRASHED
            # (first cause wins) must not be papered over.
            async def _drive_autonomous() -> int:
                rc = await _autonomous_loop(
                    inbox,
                    mission=mission,
                    drive_until=autonomous_drive_until,
                    max_turns=autonomous_max_turns,
                    kick_text=autonomous_kick_text,
                    stop=stop,
                    loop=loop,
                )
                task = convo_ref["task"]
                driver_died = (
                    task is not None
                    and task.done()
                    and (task.cancelled() or task.exception() is not None)
                )
                if not driver_died:
                    exit_cause.set_once(EXIT_ONESHOT_COMPLETE, rc)
                return rc

            autonomous_task = asyncio.create_task(_drive_autonomous())
        if oneshot_mode:
            # Foreground mode: wait for mission turn to complete, then exit.
            try:
                await convo_task
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                write_heartbeat(
                    state_dir,
                    pid=pid,
                    state=STATE_STOPPING,
                    name=name,
                    host=host,
                    writer=WRITER_SESSION_DAEMON,
                )
                rc = _write_exit()
            return rc

    try:
        await stop.wait()
    finally:
        if autonomous_task is not None and not autonomous_task.done():
            autonomous_task.cancel()
            try:
                await autonomous_task
            except (
                asyncio.CancelledError,
                Exception,
            ):  # stx-allow: fallback (reason: autonomous loop is best-effort; cancellation must not block shutdown)
                pass
        if convo_task is not None and not convo_task.done():
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(convo_task, timeout=shutdown_timeout_s)
            except (
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ):  # stx-allow: fallback (reason: runner must always reach STOPPING phase even if SDK hangs)
                convo_task.cancel()
                try:
                    await convo_task
                except (
                    asyncio.CancelledError,
                    Exception,
                ):  # stx-allow: fallback (reason: SDK surface is broad)
                    pass
        if http_task is not None and not http_task.done():
            try:
                await asyncio.wait_for(http_task, timeout=shutdown_timeout_s)
            except (
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ):  # stx-allow: fallback (reason: must stop cleanly even if uvicorn hangs)
                http_task.cancel()
                try:
                    await http_task
                except (
                    asyncio.CancelledError,
                    Exception,
                ):  # stx-allow: fallback (reason: defensive cleanup)
                    pass
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        # Final heartbeat so consumers see the clean stop, then the
        # terminal ExitRecord so they see WHY the run ended.
        write_heartbeat(
            state_dir,
            pid=pid,
            state=STATE_STOPPING,
            name=name,
            host=host,
            writer=WRITER_SESSION_DAEMON,
        )
        rc = _write_exit()
    return rc
