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
caller; humans should use ``sac start [--foreground]`` instead.
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


async def _run_conversation(
    name: str,
    state_dir: Path,
    *,
    pid: int,
    inbox: "asyncio.Queue",
    resume_session_id: str | None,
    stop: asyncio.Event,
    print_stream: bool = False,
) -> None:
    """Drive an inbox-driven conversation against ``ClaudeSDKClient``.

    Holds one ``ClaudeSDKClient`` open for the lifetime of the runner
    and drains turn envelopes from ``inbox`` serially: per turn it
    calls ``client.query(text)``, drains ``receive_response()`` into
    ``session.jsonl``, and resolves the envelope's response future
    with the concatenated assistant reply.

    Exits when a ``ShutdownEnvelope`` arrives, when ``stop`` is set, or
    on any SDK error (logged + recorded to session.jsonl).
    """
    from ._session_inbox import ShutdownEnvelope, TurnEnvelope

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
            UserMessage,
        )
    except Exception as exc:  # stx-allow: fallback (reason: optional dep import + transient init failures must downgrade to a recorded error, not crash the runner)
        logger.error("claude-agent-sdk import failed: %s", exc)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "sdk_missing", "detail": str(exc)},
        )
        _drain_failed_inbox(inbox, RuntimeError(f"sdk import: {exc}"))
        return

    try:
        from claude_agent_sdk import HookMatcher
    except Exception as exc:  # stx-allow: fallback (reason: same SDK surface as above)
        logger.error("claude-agent-sdk hook surface unavailable: %s", exc)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "sdk_missing", "detail": str(exc)},
        )
        _drain_failed_inbox(inbox, RuntimeError(f"sdk hooks: {exc}"))
        return

    from ..runtimes._sdk_common import SDKCommonError, build_sdk_options
    from ._session_hooks import build_event_log_hooks

    hooks = build_event_log_hooks(name, HookMatcher)

    try:
        options = build_sdk_options(
            name,
            permission_mode="bypassPermissions",
            resume=resume_session_id,
            hooks=hooks,
        )
    except SDKCommonError as exc:
        logger.error("could not build sdk options: %s", exc)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "options", "detail": str(exc)},
        )
        _drain_failed_inbox(inbox, exc)
        return

    async def _drive_turn(client: "ClaudeSDKClient", env: TurnEnvelope) -> None:
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

    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                env = await inbox.get()
                if isinstance(env, ShutdownEnvelope):
                    break
                if not isinstance(env, TurnEnvelope):
                    continue
                # Drive the turn unconditionally; the in-turn loop checks
                # ``stop`` and calls ``client.interrupt()`` so a SIGTERM
                # mid-stream still aborts cleanly.
                await _drive_turn(client, env)
                if env.exit_after:
                    stop.set()
                    break
                if stop.is_set():
                    break
    except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad; runner must always reach the IDLE / STOPPING phases)
        logger.exception("claude-session conversation failed for %s", name)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "sdk_runtime", "detail": str(exc)},
        )
        _drain_failed_inbox(inbox, exc)


def _drain_failed_inbox(inbox: "asyncio.Queue", exc: BaseException) -> None:
    """Resolve any pending turn futures with the failure so producers don't hang."""
    from ._session_inbox import TurnEnvelope

    while not inbox.empty():
        try:
            env = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_exception(exc)


def _safe_repr(value: object) -> str:
    """Bounded repr so a runaway tool-result blob can't bloat session.jsonl."""
    s = repr(value)
    return s if len(s) <= 1024 else s[:1024] + "…"


# Backwards-compat shim: tests + agent_meta call ``runner._build_event_log_hooks``
# directly. Re-route to the new home so the rename doesn't break them.
def _build_event_log_hooks(agent_name: str, hook_matcher_cls: Any) -> dict:
    from ._session_hooks import build_event_log_hooks

    return build_event_log_hooks(agent_name, hook_matcher_cls)


# ---------------------------------------------------------------------------
# Daemon lifecycle (signal handling, heartbeat side-task, mission turn)
# ---------------------------------------------------------------------------


async def run(
    name: str,
    *,
    state_root: Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    mission: str | None = None,
    resume_session_id: str | None = None,
    print_stream: bool = False,
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

    if mission:
        # Seed the inbox with the mission turn. exit_after=True only for
        # foreground (--print-stream) mode so the runner exits when done.
        mission_env = TurnEnvelope(
            text=mission,
            response=loop.create_future(),
            exit_after=print_stream,
        )
        await inbox.put(mission_env)
        convo_task = asyncio.create_task(
            _run_conversation(
                name,
                state_dir,
                pid=pid,
                inbox=inbox,
                resume_session_id=resume_session_id,
                stop=stop,
                print_stream=print_stream,
            )
        )
        if print_stream:
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
        if convo_task is not None and not convo_task.done():
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(convo_task, timeout=5.0)
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
        "--print-stream",
        action="store_true",
        help=(
            "Mirror assistant message chunks to stdout as they arrive, "
            "and exit when the turn completes. Used by --foreground "
            "starts so the operator sees streaming output without "
            "having to tail session.jsonl."
        ),
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
        )
    )


if __name__ == "__main__":  # pragma: no cover — exercised by adapter
    sys.exit(main())
