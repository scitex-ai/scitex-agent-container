"""Long-lived runner for the ``claude-session`` runtime (Phase 1).

This is the **daemon skeleton**. It establishes the lifecycle pattern
(state-dir layout, atomic PID file, signal handling, heartbeat) without
yet driving any SDK conversation — Phase 2 replaces the placeholder
loop body with the actual ``ClaudeSDKClient`` multi-turn loop.

Layout (per agent ``<name>``):

    $SCITEX_AGENT_CONTAINER_RUNTIME_DIR / <name> /
        pid                      one line, the runner's own PID
        heartbeat.json           {ts, pid, state}; rewritten every TICK_S
        session.jsonl            (Phase 2) one JSON object per assistant chunk

Defaults to ``~/.scitex/agent-container/runtime/`` if the env var is
unset. Matches the convention already used by ``runtimes/ssh_remote.py``
for the remote-deploy path.

Invocation:

    python -m scitex_agent_container._runners.claude_session \\
        --name <agent> [--state-root <dir>] [--tick-seconds N]

The runtime adapter (``runtimes/claude_session.py``) is the only sane
caller; humans should use ``sac start`` instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_ROOT = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
        str(Path.home() / ".scitex" / "agent-container" / "runtime"),
    )
)
DEFAULT_TICK_SECONDS = 10.0

# State-machine vocabulary used by both the runner and the runtime
# adapter's ``status`` surface. Keep tight: each value must mean exactly
# one thing to ``sac show-status`` consumers.
STATE_STARTING = "starting"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_STOPPING = "stopping"


def state_dir_for(name: str, root: Path | None = None) -> Path:
    """Return ``<state-root>/<name>``. Does not create."""
    return (root or DEFAULT_STATE_ROOT) / name


def write_pid(state_dir: Path, pid: int) -> None:
    """Write the runner's PID atomically.

    Atomic via tmp + rename so a crash mid-write never leaves a
    half-formed file (``sac show-status`` reads this concurrently).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "pid.tmp"
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    tmp.replace(state_dir / "pid")


def read_pid(state_dir: Path) -> int | None:
    """Return the recorded PID, or None if absent / unreadable."""
    p = state_dir / "pid"
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_heartbeat(state_dir: Path, *, pid: int, state: str) -> None:
    """Atomically write the heartbeat snapshot."""
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "pid": pid,
        "state": state,
    }
    tmp = state_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(state_dir / "heartbeat.json")


def read_heartbeat(state_dir: Path) -> dict | None:
    """Return the latest heartbeat dict, or None if absent / corrupt."""
    p = state_dir / "heartbeat.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def _heartbeat_loop(
    state_dir: Path,
    *,
    pid: int,
    tick_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Write heartbeat every ``tick_seconds`` until ``stop`` is set.

    First write happens immediately so consumers see the runner alive
    without waiting a full tick.
    """
    write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)


def _quota_path(state_dir: Path) -> Path:
    return state_dir / "quota.json"


def read_quota(state_dir: Path) -> dict:
    """Return the persisted quota totals, or a zeroed dict if absent."""
    p = _quota_path(state_dir)
    if not p.is_file():
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def accumulate_quota(state_dir: Path, usage: dict | None) -> dict:
    """Add one ``ResultMessage.usage`` block to the running totals.

    Returns the new totals. Atomic via tmp+rename so a concurrent
    ``sac show-status`` reader never sees a partial write.
    """
    if not usage:
        return read_quota(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    totals = read_quota(state_dir)
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
    totals["turns"] = int(totals.get("turns", 0)) + 1
    tmp = state_dir / "quota.json.tmp"
    tmp.write_text(json.dumps(totals), encoding="utf-8")
    tmp.replace(_quota_path(state_dir))
    return totals


def write_session_id(state_dir: Path, session_id: str) -> None:
    """Persist the SDK session id so a respawn can resume."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "session_id.tmp"
    tmp.write_text(session_id, encoding="utf-8")
    tmp.replace(state_dir / "session_id")


def read_session_id(state_dir: Path) -> str | None:
    """Return the persisted session id, or None if absent."""
    p = state_dir / "session_id"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def append_session_message(state_dir: Path, payload: dict) -> None:
    """Append one JSON-line record to session.jsonl."""
    state_dir.mkdir(parents=True, exist_ok=True)
    enriched = {"ts": time.time(), **payload}
    with (state_dir / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(enriched, ensure_ascii=False) + "\n")


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
    """Drive a single mission turn against ClaudeSDKClient.

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

    hooks = _build_event_log_hooks(name, HookMatcher)

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


def _build_event_log_hooks(agent_name: str, hook_matcher_cls: Any) -> dict:
    """Wire SDK hook callbacks into ``event_log.append_event``.

    The CLI runtime publishes the same event vocabulary today via
    ``sac record-hook-event`` invoked from
    ``.claude/settings.local.json``. By bridging here we keep the
    downstream schema (``pretool`` / ``posttool`` / ``prompt`` /
    ``stop`` records, same fields) identical so existing consumers
    (``sac show-status``, ``event_log.summarize``, fleet dashboards)
    work unchanged.

    Hook callbacks are *async no-ops* on the wire: they return ``{}``
    to the SDK and never block. ``append_event`` is itself swallowed-
    failures, so a misbehaving hook cannot kill the agent.
    """
    from ..event_log import append_event

    async def _on_pretool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "pretool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
            },
        )
        return {}

    async def _on_posttool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "posttool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
                "tool_response": payload.get("tool_response"),
            },
        )
        return {}

    async def _on_prompt(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "prompt",
            {"prompt": payload.get("prompt", "")},
        )
        return {}

    async def _on_stop(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "stop",
            {"stop_hook_active": bool(payload.get("stop_hook_active"))},
        )
        return {}

    return {
        "PreToolUse": [hook_matcher_cls(hooks=[_on_pretool])],
        "PostToolUse": [hook_matcher_cls(hooks=[_on_posttool])],
        "UserPromptSubmit": [hook_matcher_cls(hooks=[_on_prompt])],
        "Stop": [hook_matcher_cls(hooks=[_on_stop])],
    }


def _safe_repr(value: object) -> str:
    """Bounded repr so a runaway tool-result blob can't bloat session.jsonl."""
    s = repr(value)
    return s if len(s) <= 1024 else s[:1024] + "…"


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
    against it (Phase 2 happy path); afterward it idles awaiting
    SIGTERM. With no mission, the runner just heartbeats — useful for
    lifecycle correctness checks and for hand-driven manual sessions.
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
