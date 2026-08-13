"""Command-line entrypoint for the OpenAI-family session runner.

Split out of :mod:`openai_session` because that module crossed the repo's
512-line cap once this entrypoint landed on top of it. The split follows the
cap's own instruction: one cohesive responsibility per file. The session
LIBRARY stays in ``openai_session``; only argument parsing and the process
lifecycle live here.

``openai_session`` re-exports :func:`main` and :func:`_parse_argv`, so both
``python -m scitex_agent_container._runners.openai_session`` (what the
apptainer argv builder emits) and every existing import keep resolving.
"""

from __future__ import annotations

import argparse

from .openai_session import Message, OpenAIAgentsSession

def _parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    """Argparse mirror of the claude-session parser."""
    import argparse as _argparse
    from pathlib import Path as _Path

    from ._session_state import DEFAULT_TICK_SECONDS

    p = _argparse.ArgumentParser(
        prog="python -m scitex_agent_container._runners.openai_session",
        description="openai-session runner.",
    )
    p.add_argument("--name", required=True, help="Agent name (state-dir leaf).")
    p.add_argument(
        "--state-root",
        type=_Path,
        default=None,
        help="Accepted for parity; OpenAI runner has no state directory.",  # no heartbeat/PID
    )
    p.add_argument(
        "--tick-seconds",
        type=float,
        default=DEFAULT_TICK_SECONDS,
        help="Accepted for parity; no heartbeat loop.",  # no daemon loop
    )
    p.add_argument(
        "--mission",
        type=str,
        default=None,
        help="Initial user prompt (first user message).",
    )
    p.add_argument(
        "--resume-session-id",
        type=str,
        default=None,
        help="Accepted for parity; maps to session_id on the state db.",  # maps to session_id
    )
    p.add_argument(
        "--a2a-port",
        type=int,
        default=None,
        help="Accepted for parity; no HTTP sidecar.",  # no HTTP server
    )
    p.add_argument(
        "--a2a-host",
        type=str,
        default="127.0.0.1",
        help="Accepted for parity; no a2a-port sidecar.",  # no HTTP server
    )
    p.add_argument(
        "--a2a-card-yaml",
        type=str,
        default="",
        help="Accepted for parity; no card publishing.",  # no HTTP server
    )
    p.add_argument(
        "--channels",
        action="append",
        default=None,
        metavar="CHANNEL",
        help="Accepted for parity; channel wiring is claude-SDK-specific.",  # no SDK channel system
    )
    p.add_argument(
        "--print-stream",
        action="store_true",
        help="Mirror assistant text to stdout.",
    )
    p.add_argument(
        "--autonomous-enabled",
        action="store_true",
        help="Accepted for parity; no autonomous loop.",  # single-turn mission only
    )
    p.add_argument(
        "--autonomous-drive-until",
        type=str,
        default="DONE",
        help="Accepted for parity; no autonomous loop.",  # single-turn mission only
    )
    p.add_argument(
        "--autonomous-max-turns",
        type=int,
        default=50,
        help="Turn cap; forwarded to OpenAIAgentsSession.max_turns.",
    )
    p.add_argument(
        "--autonomous-kick-text",
        type=str,
        default="Continue. Print DONE when finished.",
        help="Accepted for parity; no autonomous loop.",  # single-turn mission only
    )
    p.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help="Accepted for parity; no restart logic.",  # no supervisor loop
    )
    p.add_argument(
        "--restart-backoff-s",
        type=float,
        default=1.0,
        help="Accepted for parity; no restart logic.",  # no supervisor loop
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — construct an :class:`OpenAIAgentsSession`, run one
    mission turn (if any), then tear down."""
    import sys

    args = _parse_argv(argv)

    # BOOT ASSERTION — same contract as claude_session's main().
    from .._maintenance._venv_dist_assertion import assert_venv_distributions_unique

    assert_venv_distributions_unique(args.name)

    session = OpenAIAgentsSession(
        args.name,
        session_id=args.resume_session_id or args.name,
        max_turns=args.autonomous_max_turns
        if args.autonomous_enabled
        else None,
    )

    async def _run() -> int:
        print_stream = args.print_stream
        await session.start()
        try:
            if args.mission:
                async for event in session.send(
                    Message(role="user", content=args.mission)
                ):
                    if print_stream and event.kind == "text_delta":
                        sys.stdout.write(event.text)
                        sys.stdout.flush()
                    if event.kind == "result":
                        return 0
                    if event.kind == "error":
                        print(f"error: {event.error}", file=sys.stderr)
                        return 1
                return 0
            else:
                return 0
        finally:
            await session.close()

    return asyncio.run(_run())


