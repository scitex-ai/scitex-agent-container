"""``sac take-next-item`` — the Stop hook that converts a stop into work.

Wired into every agent's deployed ``.claude/settings.json`` by
:mod:`~scitex_agent_container.runtimes.settings_json`::

    "Stop": [{"matcher": "", "hooks": [
        {"type": "command", "command": "scitex-agent-container take-next-item"}
    ]}]

Protocol (Claude Code hooks reference, "Stop decision control"): a Stop hook
exits 0 and prints JSON on stdout; ``{"decision": "block", "reason": "..."}``
prevents the stop and feeds ``reason`` back to Claude as its next
instruction. Printing nothing allows the stop.

We use the JSON form rather than exit 2 (which also blocks, via stderr) for
two reasons: the docs require ``reason`` to be present when blocking, and
JSON lets us attach ``systemMessage`` so a fail-open or an alarm is visible
to the operator instead of buried in the debug log.

Because stdout IS the protocol, this command prints the JSON object and
nothing else; every diagnostic goes to stderr.
"""

from __future__ import annotations

import json
import sys

import click

from .._never_stop._decide import decide
from .._never_stop._detector import probe
from .._never_stop._identity import resolve_agent


def _drain_stdin() -> dict:
    """Read the Stop-hook payload so the hook never blocks on a full pipe.

    The decision comes from the board, not from this payload, so its
    contents are optional — but it MUST be consumed. ``stop_hook_active`` is
    read only for the debug log: it is true for ANY stop-hook continuation
    (including other hooks'), so it cannot serve as our loop guard. The
    authority on consecutive re-drives is our own per-agent counter.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read() or ""
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: stdin quirks in the hook harness must never break the gate)
        return {}
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:  # stx-allow: fallback (reason: a malformed hook payload is not a reason to wedge the agent)
        return {}
    return payload if isinstance(payload, dict) else {}


@click.command("take-next-item")
@click.option(
    "--agent",
    "agent_flag",
    default="",
    help="Override the agent id resolved from the environment.",
)
def take_next_item(agent_flag: str) -> None:
    """Stop hook: take the next runnable board item instead of going idle.

    \b
    Exit 0 + no stdout  → the stop is allowed.
    Exit 0 + {"decision":"block","reason":...} → the stop becomes the next item.

    Fails OPEN (allows the stop, logs loudly) when the detector is missing,
    times out, crashes, or when this agent's identity cannot be resolved
    from the environment. Identity is NEVER derived from the working
    directory.
    """
    payload = _drain_stdin()
    agent = resolve_agent(agent_flag)

    # stx-allow: fallback (reason: this gate runs on EVERY turn end; any unhandled defect must fail open and let the agent stop, never trap it in an unstoppable loop)
    try:
        verdict = probe(agent)
        decision = decide(agent, verdict)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment above)
        print(
            f"never-stop: gate crashed ({exc!r}); allowing the stop (fail-open)",
            file=sys.stderr,
        )
        return

    if decision.log:
        if payload.get("stop_hook_active"):
            print("never-stop: (continuing from a prior stop hook)", file=sys.stderr)
        print(decision.log, file=sys.stderr)

    out: dict = {}
    if decision.block:
        out["decision"] = "block"
        out["reason"] = decision.reason
    if decision.system_message:
        out["systemMessage"] = decision.system_message

    if out:
        # stdout is the protocol — the JSON object and nothing else.
        sys.stdout.write(json.dumps(out))


__all__ = ["take_next_item"]
