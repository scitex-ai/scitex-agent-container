"""sac-statusline: Claude Code statusline command that persists the JSON payload.

Claude Code calls this command on each status-line update, piping a JSON payload
via stdin that contains context usage, rate-limits, model info, and session
details. We tee the payload to a per-agent JSON file so ``sac status`` can
report authoritative context data instead of the 1M-token JSONL approximation.

If claude-hud is installed, display is delegated to it. Otherwise a minimal
single-line fallback is printed.

Usage (written by setup_settings_json into .claude/settings.local.json):
    { "statusLine": { "type": "command", "command": "sac-statusline" } }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_STATE_DIR = Path.home() / ".scitex" / "agent-container" / "statusline"


def _agent_name() -> str:
    return (
        os.environ.get("SCITEX_AGENT_CONTAINER_AGENT")
        or os.environ.get("CLAUDE_AGENT_ID")
        or "unknown"
    )


def _persist(raw: bytes, agent: str) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_DIR / f"{agent}.json.tmp"
    out = _STATE_DIR / f"{agent}.json"
    # stx-allow: fallback (reason: disk-full or permission error must not crash
    # the Claude Code statusLine handler — missing persist is non-fatal)
    try:
        tmp.write_bytes(raw)
        tmp.rename(out)
    except OSError:
        pass


def _fallback_display(raw: bytes) -> None:
    # stx-allow: fallback (reason: statusLine display must never raise; corrupt
    # or unexpected payload shape silently outputs nothing rather than aborting)
    try:
        data = json.loads(raw)
        ctx_pct = (data.get("context_window") or {}).get("used_percentage", 0)
        model = (data.get("model") or {}).get("display_name", "")
        parts = [f"ctx:{ctx_pct:.0f}%"]
        if model:
            parts.insert(0, model)
        rl = data.get("rate_limits") or {}
        fh = rl.get("five_hour") or {}
        fh_pct = fh.get("used_percentage")
        if fh_pct is not None:
            parts.append(f"5h:{fh_pct:.0f}%")
        print(" | ".join(parts), flush=True)
    except Exception:
        pass


def main() -> None:
    """Entry point for the sac-statusline command."""
    raw = sys.stdin.buffer.read()
    agent = _agent_name()
    _persist(raw, agent)

    # Delegate display to claude-hud if available.
    # stx-allow: fallback (reason: claude-hud is an optional user-scope plugin;
    # FileNotFoundError means it is not installed — fall through to minimal echo)
    try:
        result = subprocess.run(["claude-hud"], input=raw)
        sys.exit(result.returncode)
    except FileNotFoundError:
        pass

    _fallback_display(raw)


def read_statusline_json(agent_name: str) -> dict | None:
    """Read the last persisted statusline payload for ``agent_name``.

    Returns ``None`` if no file exists or the file is unreadable/stale.
    Callers should treat ``None`` as "no authoritative data, fall back".
    """
    path = _STATE_DIR / f"{agent_name}.json"
    if not path.exists():
        return None
    # stx-allow: fallback (reason: corrupt or truncated JSON returns None so
    # callers fall back to the JSONL approximation — no data beats wrong data)
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
