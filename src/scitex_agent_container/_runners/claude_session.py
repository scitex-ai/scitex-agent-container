"""Long-lived runner for the ``runtime: claude-session`` agent path.

Per the v4 design ("sac owns the process; a harness owns only the
turn") this module owns only the **Claude harness seam**: it supplies
the Claude-specific turn driver (``_session_conversation.
run_conversation``) to the shared residency daemon in
:mod:`.session_daemon`, which owns the lifecycle — PID file write,
SIGTERM/SIGINT handling, the heartbeat side-task, the turn inbox, the
optional A2A sidecar, and clean shutdown. The IO surface (state-dir
paths, atomic file writes, heartbeat / quota helpers) lives in
:mod:`._session_state`. The hook bridge to ``event_log`` lives in
:mod:`._session_hooks`. This file re-exports the public names from
those siblings so existing consumers (``runtimes/claude_session.py``,
``agent_meta.py``, the test suite) keep their
``runner.write_pid(...)`` / ``runner.read_quota(...)`` call shapes.

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

import sys
from pathlib import Path
from typing import Any

# Re-export the IO surface so callers keep using `runner.write_pid(...)`
# etc. unchanged after the split.
from ._session_state import (
    DEFAULT_STATE_ROOT,
    DEFAULT_TICK_SECONDS,
    STATE_BUSY,
    STATE_IDLE,
    STATE_READY,
    STATE_STARTING,
    STATE_STOPPING,
    STATE_WORKING,
    accumulate_quota,
    append_session_message,
    read_heartbeat,
    read_pid,
    read_quota,
    read_session_id,
    read_started_at,
    state_dir_for,
    write_heartbeat,
    write_pid,
    write_session_id,
    write_started_at,
)
from ._session_state import (
    heartbeat_loop as _heartbeat_loop,
)

__all__ = [
    "_heartbeat_loop",
    "DEFAULT_STATE_ROOT",
    "DEFAULT_TICK_SECONDS",
    "STATE_BUSY",
    "STATE_IDLE",
    "STATE_READY",
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
    "read_started_at",
    "run",
    "state_dir_for",
    "write_heartbeat",
    "write_pid",
    "write_session_id",
    "write_started_at",
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
# Daemon lifecycle — extracted to :mod:`.session_daemon` (v4 step 3).
# ``_autonomous_loop`` is re-imported so ``runner._autonomous_loop`` keeps
# its call shape for existing consumers.
# ---------------------------------------------------------------------------


from .session_daemon import _autonomous_loop, run_session_daemon

__all__ += ["_autonomous_loop", "run_session_daemon"]


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
    channels: list[str] | None = None,
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

    Thin wrapper over :func:`.session_daemon.run_session_daemon`: the
    only Claude-specific contribution is the default turn driver
    (``_session_conversation.run_conversation``); ``run_conversation_fn``
    remains the test seam that overrides it.
    """
    turn_driver = (
        run_conversation_fn if run_conversation_fn is not None else _run_conversation
    )
    return await run_session_daemon(
        name,
        turn_driver=turn_driver,
        state_root=state_root,
        tick_seconds=tick_seconds,
        mission=mission,
        resume_session_id=resume_session_id,
        print_stream=print_stream,
        a2a_host=a2a_host,
        a2a_port=a2a_port,
        a2a_card_yaml=a2a_card_yaml,
        channels=channels,
        autonomous_enabled=autonomous_enabled,
        autonomous_drive_until=autonomous_drive_until,
        autonomous_max_turns=autonomous_max_turns,
        autonomous_kick_text=autonomous_kick_text,
        max_restarts=max_restarts,
        restart_backoff_s=restart_backoff_s,
        serve_inbound_fn=serve_inbound_fn,
        shutdown_timeout_s=shutdown_timeout_s,
    )


# ---------------------------------------------------------------------------
# CLI entry — extracted to ._session_cli to keep this module under the
# per-file line cap. Re-exported so ``runner._parse_argv`` / ``runner.main``
# and ``python -m ..._runners.claude_session`` keep their call shapes.
# ---------------------------------------------------------------------------


from ._session_cli import _parse_argv, main

__all__ += ["_parse_argv", "main"]


if __name__ == "__main__":  # pragma: no cover — exercised by adapter
    sys.exit(main())
