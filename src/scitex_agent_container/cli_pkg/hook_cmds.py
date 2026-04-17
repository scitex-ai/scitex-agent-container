"""Claude Code hook-event handler subcommand.

Receives a Claude Code hook payload on stdin (JSON) and appends it to
the per-agent event ring-buffer via :mod:`event_log`. Intended to be
wired in the agent's ``.claude/settings.local.json``::

    "hooks": {
      "PreToolUse":        [{"matcher": "", "hooks": [
        {"type": "command", "command": "scitex-agent-container hook-event pretool"}
      ]}],
      "PostToolUse":       [...hook-event posttool],
      "UserPromptSubmit":  [...hook-event prompt],
      "Stop":              [...hook-event stop]
    }

The agent name is resolved in this order:

  1. ``--agent`` CLI flag
  2. ``SCITEX_OROCHI_AGENT`` env var
  3. ``CLAUDE_AGENT_ID`` env var
  4. basename of the current working directory

The handler **never** fails loudly: any error is swallowed so that a
broken event log cannot block the agent's tool call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ..event_log import append_event


def _resolve_agent(flag: str) -> str:
    if flag:
        return flag
    for key in ("SCITEX_OROCHI_AGENT", "CLAUDE_AGENT_ID"):
        val = os.environ.get(key)
        if val:
            return val
    try:
        return Path.cwd().name or "anonymous-agent"
    except Exception:
        return "anonymous-agent"


@click.command("hook-event")
@click.argument(
    "kind",
    type=click.Choice(
        ["pretool", "posttool", "prompt", "stop", "other"],
        case_sensitive=False,
    ),
)
@click.option(
    "--agent",
    "agent_flag",
    default="",
    help="Override the resolved agent name.",
)
def hook_event(kind: str, agent_flag: str) -> None:
    """Append a Claude Code hook event to the per-agent ring-buffer."""
    try:
        raw = sys.stdin.read() or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:500]}
        agent = _resolve_agent(agent_flag)
        append_event(agent, kind.lower(), payload)
    except Exception:
        # Hooks must never break the host; swallow all failures.
        pass
    # Returning cleanly is required — Claude Code treats non-zero exit
    # as a hook failure, which aborts the tool use for PreToolUse.
    return
