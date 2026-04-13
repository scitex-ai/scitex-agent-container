"""Rich agent metadata collection (claude-hud-style).

Canonical source of truth for the metadata payload that is:
  1. Emitted by ``scitex-agent-container status <name> --json``.
  2. POSTed by the MCP sidecar heartbeat to ``/api/agents/register/``.

Ported 2026-04-12 from
``~/.scitex/orochi/agents/mamba-healer-mba/scripts/agent_meta.py``
so collection logic lives in one place.

Every field is best-effort: any failure leaves the field as its default
(``""``, ``0``, ``0.0``, ``[]``) and never raises. The caller merges this
dict on top of the base ``agent_status`` result.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .claude_usage import fetch_usage


def detect_multiplexer(session: str) -> str:
    """Return 'tmux', 'screen', or '' if neither reports the session."""
    try:
        if subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
        ).returncode == 0:
            return "tmux"
    except FileNotFoundError:
        pass
    try:
        r = subprocess.run(
            ["screen", "-ls", session],
            capture_output=True,
            text=True,
        )
        if session in r.stdout:
            return "screen"
    except FileNotFoundError:
        pass
    return ""


def _encode_claude_project(workdir: str) -> str:
    """Replicate Claude Code's cwd -> projects dir name encoding.

    ``/`` and ``.`` both become ``-``, but triple-or-more dashes that
    come from hidden dirs (``/.foo``) are collapsed back to ``--``.
    """
    encoded = workdir.replace("/", "-").replace(".", "-")
    return re.sub(r"-{3,}", "--", encoded)


def _latest_jsonls(workdir: str) -> list[Path]:
    # Claude Code encodes the *resolved* cwd, so follow symlinks first.
    try:
        resolved = str(Path(workdir).expanduser().resolve())
    except Exception:
        resolved = workdir
    proj_dir = Path.home() / ".claude" / "projects" / _encode_claude_project(resolved)
    if not proj_dir.is_dir():
        return []
    try:
        return sorted(
            proj_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return []


def _parse_skills(workdir: str) -> list[str]:
    """Parse ```skills fenced code block from workspace CLAUDE.md."""
    skills: list[str] = []
    try:
        cmd = Path(workdir) / "CLAUDE.md"
        if cmd.is_file():
            text = cmd.read_text()
            for block in re.findall(r"```skills\n(.*?)\n```", text, re.DOTALL):
                for ln in block.splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        skills.append(ln)
    except Exception:
        pass
    return skills


def _subagent_count_from_pane(session: str, multiplexer: str) -> int:
    if multiplexer != "tmux":
        return 0
    try:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        return 0
    m = re.search(r"(\d+) local agent", pane)
    return int(m.group(1)) if m else 0


def _pids_from_session(session: str, multiplexer: str) -> tuple[int, int]:
    pid = 0
    ppid = 0
    if multiplexer != "tmux":
        return pid, ppid
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        if out:
            ppid = int(out[0])
            ps = subprocess.run(
                ["pgrep", "-P", str(ppid), "-f", "claude"],
                capture_output=True,
                text=True,
            ).stdout.strip().splitlines()
            pid = int(ps[0]) if ps else ppid
    except Exception:
        pass
    return pid, ppid


def collect_rich(
    *,
    name: str,
    workdir: str,
    session: str,
) -> dict[str, Any]:
    """Collect claude-hud-style metadata for one agent.

    Parameters
    ----------
    name:
        Agent name (used only as a fallback identifier).
    workdir:
        Absolute workspace dir for the agent (used to locate CLAUDE.md
        and the Claude Code transcript JSONL files).
    session:
        Multiplexer session name (what ``tmux has-session -t`` checks).
    """
    multiplexer = detect_multiplexer(session)

    # ---- transcript-derived fields ----------------------------------
    context_pct = 0.0
    current_tool = ""
    current_tool_input = ""
    current_task = ""
    last_user_msg = ""
    last_activity = ""
    model = ""
    started_at = ""

    jsonls = _latest_jsonls(workdir)
    if jsonls:
        try:
            earliest = min(jsonls, key=lambda p: p.stat().st_mtime)
            started_at = datetime.fromtimestamp(
                earliest.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except Exception:
            pass

        try:
            lines = jsonls[0].read_text().splitlines()[-50:]
        except Exception:
            lines = []

        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "assistant" and "message" in obj:
                msg = obj["message"]
                if not model:
                    model = msg.get("model", "")
                if not last_activity:
                    last_activity = obj.get("timestamp", "")
                u = msg.get("usage", {})
                total = (
                    u.get("input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                )
                # Opus 4.6 1M context = 1,000,000 tokens
                context_pct = round((total / 1_000_000) * 100, 1)
                break

        # Find the most recent tool_use AND its input preview, so the
        # dashboard can show "Bash: docker compose build" instead of just
        # "Bash". Per ywatanabe complaint msg 5481.
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "assistant":
                content = obj.get("message", {}).get("content", [])
                for c in content:
                    if c.get("type") == "tool_use":
                        current_tool = c.get("name", "")
                        tool_input = c.get("input", {}) or {}
                        # Heuristic preview by tool kind:
                        if current_tool == "Bash":
                            preview = tool_input.get("description") \
                                or tool_input.get("command", "")
                        elif current_tool in ("Edit", "Write", "Read"):
                            preview = tool_input.get("file_path", "")
                        elif current_tool == "Grep":
                            preview = tool_input.get("pattern", "")
                        elif current_tool == "Glob":
                            preview = tool_input.get("pattern", "")
                        elif current_tool == "Agent":
                            preview = tool_input.get("description", "") \
                                or tool_input.get("subagent_type", "")
                        elif current_tool.startswith("mcp__"):
                            preview = tool_input.get("text", "") \
                                or tool_input.get("chat_id", "") \
                                or tool_input.get("query", "")
                        else:
                            preview = ""
                        if isinstance(preview, str):
                            current_tool_input = preview[:120].strip()
                        break
                if current_tool:
                    break

        # Find the most recent USER message — gives the dashboard a
        # "what was this agent last asked to do" snippet which is more
        # meaningful than the tool name alone.
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "user" and "message" in obj:
                msg = obj["message"]
                content = msg.get("content")
                if isinstance(content, str):
                    last_user_msg = content[:200].strip()
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "text":
                                parts.append(c.get("text", ""))
                            elif c.get("type") == "tool_result":
                                # Skip tool results — they're noise here
                                pass
                    last_user_msg = " ".join(parts)[:200].strip()
                if last_user_msg:
                    break

    # current_task is the high-level "what is this agent doing": prefer
    # the tool preview, then the last user message snippet, then the bare
    # tool name. Never empty if the agent is alive.
    if current_tool and current_tool_input:
        current_task = f"{current_tool}: {current_tool_input}"
    elif current_tool:
        current_task = current_tool
    elif last_user_msg:
        current_task = last_user_msg

    # ---- process / session / skills ---------------------------------
    subagent_count = _subagent_count_from_pane(session, multiplexer)
    pid, ppid = _pids_from_session(session, multiplexer)
    skills_loaded = _parse_skills(workdir)
    machine = socket.gethostname().split(".")[0]

    # ---- Claude quota fields ----------------------------------------
    quota_5h_used_pct: float | None = None
    quota_7d_used_pct: float | None = None
    quota_5h_reset_at: str | None = None
    quota_7d_reset_at: str | None = None
    quota_from_cache: bool = False
    quota_error: str | None = None
    try:
        usage = fetch_usage()
        quota_5h_used_pct = usage.get("used_pct_5h")
        quota_7d_used_pct = usage.get("used_pct_7d")
        quota_5h_reset_at = usage.get("reset_at_5h")
        quota_7d_reset_at = usage.get("reset_at_7d")
        quota_from_cache = bool(usage.get("from_cache", False))
        quota_error = usage.get("error")
    except Exception as exc:
        quota_error = f"fetch_usage raised: {exc}"

    # ---- Machine resource metrics (psutil, optional) -----------------------
    try:
        import psutil as _psutil
        _cpu_pct = _psutil.cpu_percent(interval=None)
        _vm = _psutil.virtual_memory()
        _disk = _psutil.disk_usage("/")
        _load = _psutil.getloadavg()
        _cpu_count = _psutil.cpu_count(logical=True) or 0
        _metrics = {
            "cpu_count": _cpu_count,
            "cpu_model": "",
            "cpu_used_percent": round(_cpu_pct, 1),
            "load_avg_1m": round(_load[0], 2),
            "load_avg_5m": round(_load[1], 2),
            "load_avg_15m": round(_load[2], 2),
            "mem_used_percent": round(_vm.percent, 1),
            "mem_total_mb": round(_vm.total / 1024 / 1024, 1),
            "mem_free_mb": round(_vm.available / 1024 / 1024, 1),
            "disk_used_percent": round(_disk.percent, 1),
        }
    except Exception:
        _metrics = {}

    return {
        "multiplexer": multiplexer,
        "pid": pid,
        "ppid": ppid,
        "subagent_count": subagent_count,
        "subagents": subagent_count,  # legacy alias
        "context_pct": context_pct,
        "current_tool": current_tool,
        "current_tool_input": current_tool_input,
        "current_task": current_task,
        "last_user_msg": last_user_msg,
        "last_activity": last_activity,
        "skills_loaded": skills_loaded,
        "machine": machine,
        "workdir": workdir,
        "project": name,
        # Only override started_at if we found one; caller can decide
        # whether to prefer the registry's started_at over this one.
        "started_at_transcript": started_at,
        # model from transcript is more accurate than config.model when
        # the agent is actually running under a different model alias.
        "model_transcript": model,
        "version": os.environ.get("SCITEX_OROCHI_AGENT_META_VERSION", "0.2"),
        # ---- Claude quota fields ----------------------------------------
        "quota_5h_used_pct": quota_5h_used_pct,
        "quota_7d_used_pct": quota_7d_used_pct,
        "quota_5h_reset_at": quota_5h_reset_at,
        "quota_7d_reset_at": quota_7d_reset_at,
        "quota_from_cache": quota_from_cache,
        "quota_error": quota_error,
        # ---- Machine resource metrics (for hub /api/resources/) -------------
        "metrics": _metrics,
    }
