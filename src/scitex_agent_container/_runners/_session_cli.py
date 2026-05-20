"""CLI surface for the ``claude-session`` runtime daemon.

Split out of :mod:`._claude_session` so the orchestrator module stays
under the project's per-file line cap. Holds the argparse builder
(:func:`_parse_argv`) and the process entry point (:func:`main`), which
wires parsed args into :func:`._session_conversation`-driven
:func:`claude_session.run`.

The runtime adapter (``runtimes/claude_session.py``) is the only sane
caller; humans should use ``sac agent start [--foreground]`` instead.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ._session_state import DEFAULT_TICK_SECONDS

__all__ = ["_parse_argv", "main"]


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
        "--channels",
        action="append",
        default=None,
        metavar="CHANNEL",
        help=(
            "spec.claude.channels passthrough. Repeatable. When the set "
            "contains 'server:sac', build_sdk_options auto-registers the "
            "'sac mcp channel' stdio MCP so the long-lived SDK session "
            "subscribes to its inbox SSE and a2a_send pushes are "
            "delivered (delivered_subscriber_count >= 1). Requires "
            "--a2a-port so the sidecar knows the server's listen URL."
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
    # Import lazily from the orchestrator so the module split doesn't
    # introduce an import cycle (claude_session re-exports main from here).
    from .claude_session import run

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
            channels=args.channels,
            autonomous_enabled=args.autonomous_enabled,
            autonomous_drive_until=args.autonomous_drive_until,
            autonomous_max_turns=args.autonomous_max_turns,
            autonomous_kick_text=args.autonomous_kick_text,
            max_restarts=args.max_restarts,
            restart_backoff_s=args.restart_backoff_s,
        )
    )
