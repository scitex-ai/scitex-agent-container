"""Ring-buffer event log for Claude Code hook events.

Claude Code invokes configured commands on ``PreToolUse``,
``PostToolUse``, ``UserPromptSubmit``, and ``Stop``. We capture the
JSON payloads into a per-agent ring-buffer at
``~/.scitex/agent-container/events/<agent>.jsonl`` so downstream
consumers (``agent_meta.collect_rich``) can surface recent tool calls
/ prompts / stops without the agent itself having to act.

Design rules
------------
- **Non-agentic.** Pure file I/O plus a tiny regex on the payload.
- **Non-blocking.** Writes are O(1); a rotation pass runs only when
  the file exceeds the cap.
- **Stdlib only.** ``json``, ``pathlib``, ``fcntl`` for the advisory
  lock. No psutil / requests.
- **Fail-closed.** Any exception inside the hook handler is swallowed
  so hooks can never break the agent session.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CAP_LINES = 500
DEFAULT_ROOT = (
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".scitex")
    / "agent-container"
    / "events"
)
PREVIEW_MAX_CHARS = 300


def _agent_log_path(agent: str, root: Path | None = None) -> Path:
    base = Path(root) if root else DEFAULT_ROOT
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "-", agent or "anonymous-agent")
    return base / f"{safe}.jsonl"


def _preview_tool_input(tool_name: str, tool_input: dict | None) -> str:
    """Return a short human-readable preview for the tool input."""
    if not tool_input:
        return ""
    # stx-allow: fallback (reason: unexpected tool_input shape must not crash
    # the hook handler — empty string preview is acceptable)
    try:
        if tool_name == "Bash":
            s = tool_input.get("description") or tool_input.get("command") or ""
        elif tool_name in ("Edit", "Write", "Read"):
            s = tool_input.get("file_path") or ""
        elif tool_name in ("Grep", "Glob"):
            s = tool_input.get("pattern") or ""
        elif tool_name == "Agent":
            s = (
                tool_input.get("description")
                or tool_input.get("prompt", "")[:200]
                or tool_input.get("subagent_type", "")
            )
        elif isinstance(tool_name, str) and tool_name.startswith("mcp__"):
            s = (
                tool_input.get("text")
                or tool_input.get("chat_id")
                or tool_input.get("query")
                or ""
            )
        else:
            s = json.dumps(tool_input)[:PREVIEW_MAX_CHARS]
    except Exception:  # stx-allow: fallback (reason: tool input preview best-effort — malformed payloads must not break hook events)
        s = ""
    if not isinstance(s, str):
        s = str(s)
    return s[:PREVIEW_MAX_CHARS]


def append_event(
    agent: str, kind: str, payload: dict[str, Any], *, root: Path | None = None
) -> None:
    """Append a single hook event. Never raises."""
    try:
        path = _agent_log_path(agent, root)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
        }
        tool_name = payload.get("tool_name", "")
        if tool_name:
            record["tool"] = tool_name
            record["input_preview"] = _preview_tool_input(
                tool_name, payload.get("tool_input") or {}
            )
            # For Bash run_in_background=True we want the input intact so the
            # consumer can count background tasks. Cheaper than re-parsing.
            if tool_name == "Bash":
                record["run_in_background"] = bool(
                    (payload.get("tool_input") or {}).get("run_in_background")
                )
            if kind == "posttool":
                resp = payload.get("tool_response") or {}
                if isinstance(resp, dict):
                    content = resp.get("content") or resp.get("output") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    record["result_preview"] = str(content)[:PREVIEW_MAX_CHARS]
        if kind == "prompt":
            prompt = payload.get("prompt") or payload.get("user_prompt") or ""
            record["prompt_preview"] = str(prompt)[:PREVIEW_MAX_CHARS]
        if kind == "stop":
            record["stop_hook_active"] = bool(payload.get("stop_hook_active"))

        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line)
            finally:
                # stx-allow: fallback (reason: unlock failure after write is
                # non-fatal — other processes will time out and re-acquire)
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:  # stx-allow: fallback (reason: file lock release best-effort — flock errors must not block agent)
                    pass
        _rotate_if_large(path)
    # stx-allow: fallback (reason: fail-closed event append — hook invocations must never raise)
    except Exception:
        # Fail-closed: never break the agent session.
        pass


def _rotate_if_large(path: Path, cap: int = DEFAULT_CAP_LINES) -> None:
    # stx-allow: fallback (reason: rotation is best-effort — disk full or
    # permission error keeps the old file intact without crashing the hook)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= cap:
            return
        keep = lines[-cap:]
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except Exception:  # stx-allow: fallback (reason: log rotation is best-effort — rotation failures must not break event appends)
        pass


def read_recent(
    agent: str,
    limit: int = 50,
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the last ``limit`` event records (oldest-first)."""
    # stx-allow: fallback (reason: log file may not exist yet for new agents;
    # empty list is the correct initial state)
    try:
        path = _agent_log_path(agent, root)
        if not path.is_file():
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out: list[dict[str, Any]] = []
        for ln in lines[-limit:]:
            # stx-allow: fallback (reason: truncated line during concurrent
            # write — skip and continue collecting valid records)
            try:
                out.append(json.loads(ln))
            except Exception:  # stx-allow: fallback (reason: skip corrupt log lines — partial log damage must not block reads)
                continue
        return out
    except Exception:  # stx-allow: fallback (reason: log read failure returns empty list — missing log file is normal on first boot)
        return []


def summarize(
    agent: str,
    limit: int = 50,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Return a pre-aggregated view for the status payload.

    Keys returned::

        {
          "recent_tools":       [{ts, tool, input_preview, ...}, ...] last ``limit``
          "recent_prompts":     [{ts, prompt_preview}, ...]            last 5
          "agent_calls":        [{ts, input_preview}, ...]             last 20 Agent invocations
          "background_tasks":   [{ts, input_preview}, ...]             unresolved Bash run_in_background starts
          "counts":             {tool_name: count_in_window}
          "last_tool_at":       ISO ts of newest tool (pretool kind) — "functional" heartbeat
          "last_tool_name":     tool name for last_tool_at
          "last_mcp_tool_at":   ISO ts of newest mcp__* tool — confirms MCP sidecar route
          "last_mcp_tool_name": tool name for last_mcp_tool_at
        }
    """
    events = read_recent(agent, limit=limit, root=root)
    recent_tools: list[dict[str, Any]] = []
    recent_prompts: list[dict[str, Any]] = []
    agent_calls: list[dict[str, Any]] = []
    background_tasks: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    last_tool_at: str = ""
    last_tool_name: str = ""
    last_mcp_tool_at: str = ""
    last_mcp_tool_name: str = ""
    for ev in events:
        kind = ev.get("kind")
        if kind in ("pretool", "posttool"):
            recent_tools.append(
                {
                    "ts": ev.get("ts", ""),
                    "kind": kind,
                    "tool": ev.get("tool", ""),
                    "input_preview": ev.get("input_preview", ""),
                    "result_preview": ev.get("result_preview", ""),
                    "run_in_background": ev.get("run_in_background"),
                }
            )
            if kind == "pretool":
                t = ev.get("tool", "")
                ts = ev.get("ts", "")
                if t:
                    counts[t] = counts.get(t, 0) + 1
                    # Track newest tool-use timestamp (events are
                    # oldest-first, so plain assignment wins the last).
                    last_tool_at = ts
                    last_tool_name = t
                    if t.startswith("mcp__"):
                        last_mcp_tool_at = ts
                        last_mcp_tool_name = t
                if t == "Agent":
                    agent_calls.append(
                        {
                            "ts": ts,
                            "input_preview": ev.get("input_preview", ""),
                        }
                    )
                if t == "Bash" and ev.get("run_in_background"):
                    background_tasks.append(
                        {
                            "ts": ts,
                            "input_preview": ev.get("input_preview", ""),
                        }
                    )
        elif kind == "prompt":
            recent_prompts.append(
                {
                    "ts": ev.get("ts", ""),
                    "prompt_preview": ev.get("prompt_preview", ""),
                }
            )
    return {
        "recent_tools": recent_tools[-limit:],
        "recent_prompts": recent_prompts[-5:],
        "agent_calls": agent_calls[-20:],
        "background_tasks": background_tasks[-20:],
        "counts": counts,
        "last_tool_at": last_tool_at,
        "last_tool_name": last_tool_name,
        "last_mcp_tool_at": last_mcp_tool_at,
        "last_mcp_tool_name": last_mcp_tool_name,
    }
