"""Adapt a Claude-Code hook's stdout for Codex's hooks engine (in-container).

Measured on the first codex turn that ran the fleet's hooks (handyman-01,
2026-09-05 09:43 UTC): Codex reported

    PreToolUse hook (failed)
    error: PreToolUse hook returned updatedInput without permissionDecision:allow

for the rtk command-rewrite hook, whose output is

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecisionReason": "RTK auto-rewrite",
      "updatedInput": {"command": "rtk ..."}}}

Claude Code accepts that shape (an ``updatedInput`` implies allow); Codex
requires the decision to be explicit. This filter reads the hook's stdout,
adds ``"permissionDecision": "allow"`` when ``updatedInput`` is present and
no decision is stated, and passes everything else through untouched — a
hook that denies, or says nothing, is unchanged. Usage in a hooks.json
command: ``<hook command> | python3 -m scitex_agent_container.runtimes._codex_hook_output``.
"""

from __future__ import annotations

import json
import sys

__all__ = ["adapt_hook_output", "main"]


def adapt_hook_output(text: str) -> str:
    """Return ``text`` with an explicit allow added where Codex needs one."""
    stripped = text.strip()
    if not stripped:
        return text
    try:
        document = json.loads(stripped)
    except ValueError:
        return text
    if not isinstance(document, dict):
        return text
    specific = document.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        return text
    if "updatedInput" in specific and "permissionDecision" not in specific:
        specific["permissionDecision"] = "allow"
        return json.dumps(document)
    return text


def main() -> int:
    sys.stdout.write(adapt_hook_output(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
