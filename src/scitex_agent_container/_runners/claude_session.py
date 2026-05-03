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
    mission: str,
    resume_session_id: str | None,
    stop: asyncio.Event,
    print_stream: bool = False,
) -> None:
    """Drive a single mission turn against ``ClaudeSDKClient``.

    Streams every assistant chunk into ``session.jsonl`` and persists
    the session id once the turn completes so a respawn can resume.
    Returns when the SDK emits the closing ``ResultMessage``, when the
    caller cancels via ``stop``, or on any SDK error (logged + recorded
    to session.jsonl).
    """
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
        return

    try:
        from claude_agent_sdk import HookMatcher
    except Exception as exc:  # stx-allow: fallback (reason: same SDK surface as above)
        logger.error("claude-agent-sdk hook surface unavailable: %s", exc)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "sdk_missing", "detail": str(exc)},
        )
        return

    from ..runtimes._sdk_common import SDKCommonError, build_sdk_options
    from ._session_hooks import build_event_log_hooks

    hooks = build_event_log_hooks(name, HookMatcher)

    write_heartbeat(state_dir, pid=pid, state=STATE_WORKING)
    append_session_message(state_dir, {"type": "user", "text": mission})

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
        return

    async def _drive(client: "ClaudeSDKClient") -> None:
        await client.query(mission)
        async for msg in client.receive_response():
            if stop.is_set():
                # Graceful: ask the SDK to stop in-flight work.
                await client.interrupt()
                break
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        append_session_message(
                            state_dir, {"type": "assistant", "text": block.text}
                        )
                        if print_stream:
                            # Foreground mode: mirror assistant text to
                            # stdout so the operator sees streaming output
                            # without tailing session.jsonl.
                            sys.stdout.write(block.text)
                            sys.stdout.flush()
            elif isinstance(msg, UserMessage):
                # Tool-result echo from the SDK; record so transcripts
                # can render the full turn structure later.
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
                    {
                        "type": "result",
                        "session_id": sid,
                        "usage": usage,
                    },
                )
                break

    try:
        async with ClaudeSDKClient(options=options) as client:
            await _drive(client)
    except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad; runner must always reach the IDLE / STOPPING phases)
        logger.exception("claude-session conversation failed for %s", name)
        append_session_message(
            state_dir,
            {"type": "error", "kind": "sdk_runtime", "detail": str(exc)},
        )
    finally:
        if not stop.is_set():
            write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)


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

    if mission:
        await _run_conversation(
            name,
            state_dir,
            pid=pid,
            mission=mission,
            resume_session_id=resume_session_id,
            stop=stop,
            print_stream=print_stream,
        )
        if print_stream:
            # Foreground mode: when the conversation ends, exit cleanly
            # rather than parking in IDLE waiting for SIGTERM. The
            # operator's terminal is freed for the next command.
            return 0

    try:
        await stop.wait()
    finally:
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
