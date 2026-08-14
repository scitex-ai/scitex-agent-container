"""Command-line entrypoint for the OpenAI-family session runner.

v4 migration step 7 (card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``): this
entrypoint hands the process to the SHARED residency daemon
(:func:`.session_daemon.run_session_daemon`) with the OpenAI turn
driver (:func:`._openai_turn_driver.run_openai_conversation`) as the
harness seam. Before this step the module ran one ad-hoc mission turn
and returned, while advertising eight "accepted for parity" flags its
own comments called lies — and its ``asyncio.run`` call had no
``import asyncio`` at all (the 2e3602c4 split left the import behind in
``openai_session.py``), so every real ``python -m`` invocation
NameError'd after argparse. The daemon handoff makes the flags REAL:
``--state-root`` / ``--tick-seconds`` feed the daemon's heartbeat,
``--a2a-*`` bind the vendor-neutral inbound-turn sidecar,
``--residency`` selects the step-6 axis, and the autonomous flags drive
the daemon's harness-agnostic drive-until-done loop.

The ONE flag that stays refused is ``--resume-session-id``: the harness
registry's ``openai-agents`` descriptor declares ``can_resume=False``
(the ``SQLiteSession`` db persists turns under the agent's own name,
but sac's resume contract — rehydrate a PRIOR conversation from a
caller-supplied id — is not implemented for this harness). The refusal
reads the registry rather than hardcoding the answer, so flipping the
descriptor is the single edit that would thread the id through.

``openai_session`` re-exports :func:`main` and :func:`_parse_argv`, so
both ``python -m scitex_agent_container._runners.openai_session`` (what
the apptainer argv builder emits) and every existing import keep
resolving. The runtime adapter (``runtimes/openai_session.py``) is the
only sane caller; humans should use ``sac agents start`` instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config._residency_types import AGENT_RESIDENCIES, RESIDENT
from ._session_state import DEFAULT_TICK_SECONDS

logger = logging.getLogger(__name__)

__all__ = ["_parse_argv", "main"]


def _parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    """Argparse mirror of the claude-session parser (shared daemon argv)."""
    p = argparse.ArgumentParser(
        prog="python -m scitex_agent_container._runners.openai_session",
        description="openai-session runtime daemon.",
    )
    p.add_argument("--name", required=True, help="Agent name (state-dir leaf).")
    p.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help=(
            "Override the per-agent state root "
            "(default: $SCITEX_AGENT_CONTAINER_RUNTIME_DIR)."
        ),
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
            "Initial user prompt. With this flag the daemon drives one "
            "conversation turn and then honours the residency axis. "
            "Without it the daemon just heartbeats."
        ),
    )
    p.add_argument(
        "--resume-session-id",
        type=str,
        default=None,
        help=(
            "REFUSED for this harness: the registry's openai-agents "
            "descriptor declares can_resume=False (exit 2, error names "
            "the contract)."
        ),
    )
    p.add_argument(
        "--a2a-port",
        type=int,
        default=None,
        help=(
            "If set, serve the inbound-turn endpoint on this port. "
            'POST /v1/turn with {"text": "..."} drops the prompt onto '
            "the runner's persistent conversation and returns the reply."
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
            "/.well-known/agent-card.json so peers can discover the "
            "agent's capabilities."
        ),
    )
    p.add_argument(
        "--channels",
        action="append",
        default=None,
        metavar="CHANNEL",
        help=(
            "spec.claude.channels passthrough. Channel adapters are "
            "Claude-SDK-specific; the OpenAI turn driver warns LOUDLY "
            "and ignores them rather than degrading silently."
        ),
    )
    p.add_argument(
        "--residency",
        type=str,
        choices=sorted(AGENT_RESIDENCIES),
        default=RESIDENT,
        help=(
            "spec.residency passthrough (v4 residency axis). 'resident' "
            "(default): the daemon parks awaiting more work after a "
            "conversation completes. 'one-shot': the daemon exits "
            "cleanly (ExitRecord reason oneshot-complete) when its "
            "conversation completes."
        ),
    )
    p.add_argument(
        "--print-stream",
        action="store_true",
        help=(
            "Mirror assistant text chunks to stdout as they arrive, and "
            "exit when the mission turn completes (--foreground starts)."
        ),
    )
    p.add_argument(
        "--autonomous-enabled",
        action="store_true",
        help=(
            "Drive turns until --autonomous-drive-until matches an "
            "assistant reply or --autonomous-max-turns is reached. "
            "Requires --mission. The loop is the daemon's own, "
            "harness-agnostic."
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
        help="Cap on autonomous turns. Hitting it without a match exits non-zero.",
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
            "Accepted per the shared runner argv; the OpenAI turn driver "
            "has no persistent vendor client to supervise-restart (each "
            "turn is an independent chat-completions exchange)."
        ),
    )
    p.add_argument(
        "--restart-backoff-s",
        type=float,
        default=1.0,
        help="Accepted per the shared runner argv; see --max-restarts.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, turn_driver: Any | None = None) -> int:
    """CLI entry point — run the shared session daemon with the OpenAI driver.

    ``turn_driver`` is the test seam (mirrors ``claude_session.run``'s
    ``run_conversation_fn``): ``None`` selects the production
    :func:`._openai_turn_driver.run_openai_conversation`.
    """
    args = _parse_argv(argv)

    # BOOT ASSERTION — same overlay-venv contract as claude_session's main().
    from .._maintenance._venv_dist_assertion import assert_venv_distributions_unique

    assert_venv_distributions_unique(args.name)

    # Registry-derived resume gate (v4 step 4 descriptor contract): the
    # answer lives in the openai-agents entry, not in this file.
    from ..config._harness_registry import HARNESS_DESCRIPTORS, OPENAI_AGENTS

    descriptor = HARNESS_DESCRIPTORS[OPENAI_AGENTS]
    if args.resume_session_id and not descriptor.can_resume:
        logger.error(
            "refusing --resume-session-id=%s: harness %r declares "
            "can_resume=False in the harness registry "
            "(config/_harness_registry.py) — this harness cannot resume a "
            "prior conversation by id. Start without the flag instead.",
            args.resume_session_id,
            OPENAI_AGENTS,
        )
        return 2

    if turn_driver is None:
        from ._openai_turn_driver import run_openai_conversation as turn_driver

    from .session_daemon import run_session_daemon

    return asyncio.run(
        run_session_daemon(
            args.name,
            turn_driver=turn_driver,
            residency=args.residency,
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
