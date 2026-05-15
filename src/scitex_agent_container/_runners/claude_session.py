"""Long-lived runner for the ``runtime: claude-session`` agent path.

This module owns the **lifecycle**: PID file write, SIGTERM/SIGINT
handling, the heartbeat side-task, the SDK conversation, and clean
shutdown. The IO surface (state-dir paths, atomic file writes,
heartbeat / quota helpers) lives in :mod:`._session_state`. The hook
bridge to ``event_log`` lives in :mod:`._session_hooks`. This file
re-exports the public names from those siblings so existing
consumers (``runtimes/claude_session.py``, ``agent_meta.py``, the
test suite) keep their ``runner.write_pid(...)`` / ``runner.read_quota(...)``
call shapes.

State layout (per agent ``<name>``) — see ``_session_state`` for details:

    $SCITEX_AGENT_CONTAINER_RUNTIME_DIR / <name> /
        pid                      one line, the runner's own PID
        heartbeat.json           {ts, pid, state}; refreshed every TICK_S
        session.jsonl            one JSON object per turn event
        session_id               persisted SDK session id (resume marker)
        quota.json               accumulated per-turn token totals

Invocation:

    python -m scitex_agent_container._runners.claude_session \\
        --name <agent>
        [--state-root <dir>]
        [--tick-seconds N]
        [--mission "<prompt>"]
        [--resume-session-id <uuid>]
        [--print-stream]

The runtime adapter (``runtimes/claude_session.py``) is the only sane
caller; humans should use ``sac agent start [--foreground]`` instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

# Re-export the IO surface so callers keep using `runner.write_pid(...)`
# etc. unchanged after the split.
from ._session_state import (
    DEFAULT_STATE_ROOT,
    DEFAULT_TICK_SECONDS,
    STATE_IDLE,
    STATE_STARTING,
    STATE_STOPPING,
    STATE_WORKING,
    accumulate_quota,
    append_session_message,
    read_heartbeat,
    read_pid,
    read_quota,
    read_session_id,
    state_dir_for,
    write_heartbeat,
    write_pid,
    write_session_id,
)
from ._session_state import (
    heartbeat_loop as _heartbeat_loop,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_STATE_ROOT",
    "DEFAULT_TICK_SECONDS",
    "STATE_IDLE",
    "STATE_STARTING",
    "STATE_STOPPING",
    "STATE_WORKING",
    "accumulate_quota",
    "append_session_message",
    "main",
    "read_heartbeat",
    "read_pid",
    "read_quota",
    "read_session_id",
    "run",
    "state_dir_for",
    "write_heartbeat",
    "write_pid",
    "write_session_id",
]


# ---------------------------------------------------------------------------
# SDK conversation
# ---------------------------------------------------------------------------


from ._session_conversation import (
    _drain_failed_inbox,
    _safe_repr,
)
from ._session_conversation import (
    run_conversation as _run_conversation,
)

__all__ += ["_drain_failed_inbox", "_safe_repr", "_run_conversation"]


# Backwards-compat shim: tests + agent_meta call ``runner._build_event_log_hooks``
# directly. Re-route to the new home so the rename doesn't break them.
def _build_event_log_hooks(
    agent_name: str,
    hook_matcher_cls: Any,
    *,
    event_log_root: Any | None = None,
) -> dict:
    from ._session_hooks import build_event_log_hooks

    return build_event_log_hooks(
        agent_name, hook_matcher_cls, event_log_root=event_log_root
    )


# ---------------------------------------------------------------------------
# Daemon lifecycle (signal handling, heartbeat side-task, mission turn)
# ---------------------------------------------------------------------------


async def _autonomous_loop(
    inbox: "asyncio.Queue",
    *,
    mission: str,
    drive_until: str,
    max_turns: int,
    kick_text: str,
    stop: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> int:
    """Drive turns until ``drive_until`` matches an assistant reply or
    ``max_turns`` is reached.

    Returns 0 on a clean ``drive_until`` match, 1 if the cap is hit
    without a match. Always sets ``stop`` before returning so the
    surrounding daemon shuts down cleanly.

    F-CS3 phase 2 — pairs with the schema landed in phase 1
    (``spec.autonomous`` in agent yaml).
    """
    from ._session_inbox import TurnEnvelope

    text = mission
    rc = 1
    for _ in range(max(1, max_turns)):
        if stop.is_set():
            break
        env = TurnEnvelope(text=text, response=loop.create_future(), exit_after=False)
        await inbox.put(env)
        try:
            reply = await env.response
        except Exception:  # stx-allow: fallback (reason: convo task may fail mid-loop; treat as terminal — set stop and exit non-zero)
            break
        if drive_until and drive_until in (reply or ""):
            rc = 0
            break
        text = kick_text
    stop.set()
    return rc


async def run(
    name: str,
    *,
    state_root: Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    mission: str | None = None,
    resume_session_id: str | None = None,
    print_stream: bool = False,
    a2a_host: str = "127.0.0.1",
    a2a_port: int | None = None,
    a2a_card_yaml: str = "",
    autonomous_enabled: bool = False,
    autonomous_drive_until: str = "DONE",
    autonomous_max_turns: int = 50,
    autonomous_kick_text: str = "Continue. Print DONE when finished.",
    max_restarts: int = 0,
    restart_backoff_s: float = 1.0,
    run_conversation_fn: Any | None = None,
    serve_inbound_fn: Any | None = None,
    shutdown_timeout_s: float = 5.0,
) -> int:
    """Run the daemon loop until SIGTERM / SIGINT.

    Returns the exit code (0 on clean shutdown). Idempotent re-entry is
    *not* attempted — the adapter is responsible for ensuring at most
    one runner per name.

    If ``mission`` is given, the runner drives one SDK conversation turn
    against it; afterward it idles awaiting SIGTERM. With no mission,
    the runner just heartbeats — useful for lifecycle correctness
    checks and for hand-driven manual sessions.
    """
    state_dir = state_dir_for(name, state_root)
    state_dir.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    write_pid(state_dir, pid)
    write_heartbeat(state_dir, pid=pid, state=STATE_STARTING)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(signum: int) -> None:
        logger.info("runner %s received signal %d, stopping", name, signum)
        write_heartbeat(state_dir, pid=pid, state=STATE_STOPPING)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (
            NotImplementedError
        ):  # stx-allow: fallback (reason: Windows / no asyncio signal support)
            signal.signal(sig, lambda s, _f: _on_signal(s))

    hb_task = asyncio.create_task(
        _heartbeat_loop(state_dir, pid=pid, tick_seconds=tick_seconds, stop=stop),
    )

    from ._session_inbox import ShutdownEnvelope, TurnEnvelope, make_inbox

    inbox: asyncio.Queue = make_inbox()
    convo_task: asyncio.Task | None = None
    http_task: asyncio.Task | None = None

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
            ),
        )

    # Spawn the SDK conversation task whenever the inbox has a producer:
    # mission seeds it with the boot prompt, or a2a_port lets HTTP feed
    # turns. Without a producer, no SDK client is needed.
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
        _convo_fn = (
            run_conversation_fn
            if run_conversation_fn is not None
            else _run_conversation
        )
        convo_task = asyncio.create_task(
            _convo_fn(
                name,
                state_dir,
                pid=pid,
                inbox=inbox,
                resume_session_id=resume_session_id,
                stop=stop,
                print_stream=print_stream,
                max_restarts=max_restarts,
                restart_backoff_s=restart_backoff_s,
            )
        )
        if mission and autonomous_enabled:
            # F-CS3 phase 2: drive turns until drive_until matches or
            # max_turns is reached. The loop sets ``stop`` itself, so
            # the surrounding shutdown path handles cleanup uniformly.
            autonomous_task = asyncio.create_task(
                _autonomous_loop(
                    inbox,
                    mission=mission,
                    drive_until=autonomous_drive_until,
                    max_turns=autonomous_max_turns,
                    kick_text=autonomous_kick_text,
                    stop=stop,
                    loop=loop,
                )
            )
        if mission and print_stream and not autonomous_enabled:
            # Foreground mode: wait for mission turn to complete, then exit.
            try:
                await convo_task
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                write_heartbeat(state_dir, pid=pid, state=STATE_STOPPING)
            return 0

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
        # Final heartbeat so consumers see the clean stop.
        write_heartbeat(state_dir, pid=pid, state=STATE_STOPPING)
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scitex_agent_container._runners.claude_session",
        description="claude-session runtime daemon.",
    )
    p.add_argument("--name", required=True, help="Agent name (state-dir leaf).")
    p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Override the per-agent state root (default: $SCITEX_AGENT_CONTAINER_RUNTIME_DIR).",
    )
    p.add_argument(
        "--tick-seconds",
        type=float,
        default=DEFAULT_TICK_SECONDS,
        help="Heartbeat interval in seconds (default: 10).",
    )
    p.add_argument(
        "--mission",
        type=str,
        default=None,
        help=(
            "Initial user prompt. With this flag the runner drives one "
            "SDK conversation turn and then idles awaiting SIGTERM. "
            "Without it the runner just heartbeats."
        ),
    )
    p.add_argument(
        "--resume-session-id",
        type=str,
        default=None,
        help=(
            "Resume a prior SDK session (UUID from a previous run's "
            "session_id state file). Forwarded to ClaudeAgentOptions(resume=...)."
        ),
    )
    p.add_argument(
        "--a2a-port",
        type=int,
        default=None,
        help=(
            "If set, serve an HTTP inbound-turn endpoint on this port. "
            'POST /v1/turn with {"text": "..."} drops the prompt onto '
            "the runner's persistent SDK conversation and returns the reply."
        ),
    )
    p.add_argument(
        "--a2a-host",
        type=str,
        default="127.0.0.1",
        help="Bind address for --a2a-port (default: 127.0.0.1, loopback only).",
    )
    p.add_argument(
        "--a2a-card-yaml",
        type=str,
        default="",
        help=(
            "Path (in-container) to the agent's spec.yaml. When set, the "
            "sidecar publishes the A2A AgentCard at "
            "/.well-known/agent-card.json (and /.well-known/agent.json) "
            "so peers can discover the agent's capabilities."
        ),
    )
    p.add_argument(
        "--print-stream",
        action="store_true",
        help=(
            "Mirror assistant message chunks to stdout as they arrive, "
            "and exit when the turn completes. Used by --foreground "
            "starts so the operator sees streaming output without "
            "having to tail session.jsonl."
        ),
    )
    # F-CS3 phase 2 — autonomous drive-until-done.
    p.add_argument(
        "--autonomous-enabled",
        action="store_true",
        help=(
            "Drive turns until --autonomous-drive-until matches an "
            "assistant reply or --autonomous-max-turns is reached. "
            "Requires --mission. Mirrors spec.autonomous in agent yaml."
        ),
    )
    p.add_argument(
        "--autonomous-drive-until",
        type=str,
        default="DONE",
        help="Substring; assistant reply containing it ends the loop with exit 0.",
    )
    p.add_argument(
        "--autonomous-max-turns",
        type=int,
        default=50,
        help="Cap on turns. Hitting it without a match exits non-zero.",
    )
    p.add_argument(
        "--autonomous-kick-text",
        type=str,
        default="Continue. Print DONE when finished.",
        help="Text submitted as the next user turn after a non-matching reply.",
    )
    p.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help=(
            "Supervisor restart cap: how many times to re-open the SDK "
            "client after a mid-session crash (default 0 = no restart)."
        ),
    )
    p.add_argument(
        "--restart-backoff-s",
        type=float,
        default=1.0,
        help="Initial supervisor backoff seconds; doubles each retry.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_argv(argv)
    return asyncio.run(
        run(
            args.name,
            state_root=args.state_root,
            tick_seconds=args.tick_seconds,
            mission=args.mission,
            resume_session_id=args.resume_session_id,
            print_stream=args.print_stream,
            a2a_host=args.a2a_host,
            a2a_port=args.a2a_port,
            a2a_card_yaml=args.a2a_card_yaml,
            autonomous_enabled=args.autonomous_enabled,
            autonomous_drive_until=args.autonomous_drive_until,
            autonomous_max_turns=args.autonomous_max_turns,
            autonomous_kick_text=args.autonomous_kick_text,
            max_restarts=args.max_restarts,
            restart_backoff_s=args.restart_backoff_s,
        )
    )


if __name__ == "__main__":  # pragma: no cover — exercised by adapter
    sys.exit(main())
