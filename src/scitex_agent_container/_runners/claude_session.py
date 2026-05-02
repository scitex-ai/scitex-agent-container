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


async def run(
    name: str,
    *,
    state_root: Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> int:
    """Run the daemon loop until SIGTERM / SIGINT.

    Returns the exit code (0 on clean shutdown). Idempotent re-entry is
    *not* attempted — the adapter is responsible for ensuring at most
    one runner per name.
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
        description="claude-session runtime daemon (Phase 1: heartbeat only).",
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_argv(argv)
    return asyncio.run(
        run(
            args.name,
            state_root=args.state_root,
            tick_seconds=args.tick_seconds,
        )
    )


if __name__ == "__main__":  # pragma: no cover — exercised by adapter
    sys.exit(main())
