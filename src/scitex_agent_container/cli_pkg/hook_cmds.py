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
  2. ``SCITEX_AGENT_CONTAINER_AGENT`` env var
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

import re

from ..event_log import append_event

# Patterns that indicate a dangerous filesystem scan. The key danger is
# scanning from / or ~ (home root) which causes severe I/O load on HPC
# systems with millions of files (Spartan admin complaint, todo#424).
_DANGEROUS_BASH_PATTERNS: list[re.Pattern] = [
    # find / … (scan from filesystem root)
    re.compile(r"\bfind\s+/\s"),
    re.compile(r"\bfind\s+/\s*$"),
    # find ~ or $HOME (scan from home) — also match ~/... and $HOME/...
    re.compile(r"\bfind\s+~[\s/]"),
    re.compile(r"\bfind\s+~\s*$"),
    re.compile(r"\bfind\s+\$HOME[\s/]"),
    re.compile(r"\bfind\s+\$HOME\s*$"),
    # du -a / (disk usage from root)
    re.compile(r"\bdu\s+.*-a\s+/\s"),
    re.compile(r"\bdu\s+.*-a\s+/\s*$"),
    # du -a ~ (disk usage from home)
    re.compile(r"\bdu\s+.*-a\s+~[\s/]"),
    re.compile(r"\bdu\s+.*-a\s+~\s*$"),
]

_BLOCK_MESSAGE = (
    "BLOCKED by scitex-agent-container safety guard (todo#424).\n"
    "Scanning from / or ~ with find/du is prohibited on HPC systems (Spartan admin complaint).\n"
    "Use a narrower path (e.g. find . -name X, find /scratch -name X) or 'which X' / 'module avail'."
)


def _is_dangerous_bash(cmd: str) -> bool:
    for pat in _DANGEROUS_BASH_PATTERNS:
        if pat.search(cmd):
            return True
    return False


def _resolve_agent(flag: str) -> str:
    if flag:
        return flag
    for key in ("SCITEX_AGENT_CONTAINER_AGENT", "CLAUDE_AGENT_ID"):
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
    """Append a Claude Code hook event to the per-agent ring-buffer.

    For ``pretool`` events on the ``Bash`` tool, also runs the bash-guard
    (todo#424): if the command matches a dangerous-scan pattern, prints
    the block message to stdout and exits 2 so Claude Code aborts the call.
    """
    try:
        raw = sys.stdin.read() or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:500]}
        agent = _resolve_agent(agent_flag)
        append_event(agent, kind.lower(), payload)

        # Guard: block dangerous Bash commands on PreToolUse.
        if kind.lower() == "pretool":
            tool = payload.get("tool_name", "") or payload.get("tool", "")
            if tool == "Bash":
                cmd = ""
                inp = payload.get("tool_input") or {}
                if isinstance(inp, dict):
                    cmd = str(inp.get("command", ""))
                elif isinstance(inp, str):
                    cmd = inp
                if cmd and _is_dangerous_bash(cmd):
                    print(_BLOCK_MESSAGE, flush=True)
                    sys.exit(2)
    except SystemExit:
        raise  # let the exit code propagate
    except Exception:
        # Hooks must never break the host; swallow all other failures.
        pass
    # Returning cleanly is required — Claude Code treats non-zero exit
    # as a hook failure, which aborts the tool use for PreToolUse.
    return


@click.command("guard-bash")
@click.option(
    "--agent",
    "agent_flag",
    default="",
    help="Override the resolved agent name.",
)
def guard_bash(agent_flag: str) -> None:
    """Standalone PreToolUse guard — blocks dangerous Bash commands (todo#424).

    Reads a Claude Code PreToolUse JSON payload on stdin.  Exits 2 to
    block if the Bash command matches a dangerous-scan pattern (find /,
    find ~, du -a /, etc.).  Exits 0 otherwise (hook pass-through).

    Can be used as a second PreToolUse hook entry with matcher='Bash'
    alongside the existing hook-event pretool for separation of concerns.
    """
    try:
        raw = sys.stdin.read() or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

        tool = payload.get("tool_name", "") or payload.get("tool", "")
        if tool != "Bash":
            return

        cmd = ""
        inp = payload.get("tool_input") or {}
        if isinstance(inp, dict):
            cmd = str(inp.get("command", ""))
        elif isinstance(inp, str):
            cmd = inp
        if cmd and _is_dangerous_bash(cmd):
            print(_BLOCK_MESSAGE, flush=True)
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        pass
