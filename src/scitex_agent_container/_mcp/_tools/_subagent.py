"""Monitoring tools for Claude Code Agent-tool subagents.

These cover *Type 2* subagents — the ones the Claude Code Agent tool
spawns inside a single Claude Code session — as distinct from the
``sac``-managed apptainer agents (Type 1) handled by ``_agent.py``.

Claude Code writes each Agent-tool invocation's transcript to:

    ~/.claude/projects/<project_hash>/<session_uuid>/subagents/agent-<random>.jsonl

where ``project_hash`` is the absolute project path with ``/`` replaced
by ``-`` (e.g. ``-home-ywatanabe-proj-lead`` for ``/home/ywatanabe/proj/lead``).

Scope: this module is **pure state**. It walks the on-disk
transcripts, stats them, tails the last few records, and returns one
dict per matching subagent — no judgment about whether they're
``running`` / ``stale`` / ``dead`` / ``completed``. Classification is
deliberately out of scope; the caller (any orchestrator)
owns the policy of mapping these facts to a status label.

Hard constraint: ``subagent_get_state`` NEVER loads a transcript fully
into memory. Transcripts grow to hundreds of MB; we use a bounded-tail
read of the last ~64 KB and parse whatever JSON lines we can recover.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "subagent_get_state",
    "register_subagent_tools",
]


# ─── tunables (module-level so tests can poke them deterministically) ─────────

# Bytes to read from the tail of each transcript when extracting the
# last assistant/user/tool events. 64 KB is comfortably more than a
# dozen full assistant turns.
_TAIL_BYTES = 64 * 1024

# Lines to scan from the head when looking for the launch description.
# In practice the description is in line 1 (the first user message),
# but we look a little further in case a meta record precedes it.
_HEAD_LINES = 5


# ─── path resolution ─────────────────────────────────────────────────────────


def _project_hash_for(cwd: str | Path) -> str:
    """Translate an absolute path into Claude Code's project-hash form.

    ``/home/ywatanabe/proj/lead`` → ``-home-ywatanabe-proj-lead``.
    """
    s = str(Path(cwd).resolve())
    return s.replace("/", "-")


def _claude_projects_root() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


# ─── tail-only readers ───────────────────────────────────────────────────────


def _read_tail_bytes(path: Path, n: int) -> bytes:
    """Read at most ``n`` bytes from the end of ``path``. Stale-tolerant:
    returns ``b''`` if the file shrinks or vanishes mid-read."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return b""
    if size == 0:
        return b""
    n = min(n, size)
    try:
        with path.open("rb") as fh:
            fh.seek(size - n)
            return fh.read(n)
    except OSError:
        return b""


def _iter_tail_records(path: Path, *, max_bytes: int = _TAIL_BYTES) -> list[dict]:
    """Return the last set of parseable JSONL records from ``path``.

    Reads at most ``max_bytes`` from the tail, drops the first
    (possibly truncated) line if the read started mid-file, and decodes
    every remaining line that parses as JSON. Lines that fail to parse
    are skipped silently — never raises.
    """
    blob = _read_tail_bytes(path, max_bytes)
    if not blob:
        return []
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # Drop the (potentially partial) first line iff we didn't read the
    # whole file from byte 0.
    try:
        full_size = path.stat().st_size
    except FileNotFoundError:
        full_size = len(blob)
    if full_size > max_bytes and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _iter_head_records(path: Path, *, max_lines: int = _HEAD_LINES) -> list[dict]:
    """Yield up to ``max_lines`` parseable records from the head of ``path``."""
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


# ─── extractors ──────────────────────────────────────────────────────────────


def _extract_description(records: list[dict]) -> str | None:
    """Find the launch description in the first records of the transcript.

    The Agent tool's first user record's ``message.content`` (a string)
    is the prompt the launching agent passed in — the closest thing to
    a "description" we get from the wire format.
    """
    for rec in records:
        if rec.get("type") != "user":
            continue
        msg = rec.get("message") or {}
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            # Cap the description to keep dicts compact — the full
            # prompt can be many KB.
            return content.strip()[:500]
        if isinstance(content, list):
            # Some launches use the block format. Concatenate text parts.
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = b.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            if parts:
                joined = "\n".join(parts).strip()
                if joined:
                    return joined[:500]
    return None


def _extract_last_tool(records: list[dict]) -> str | None:
    """Walk tail records newest-first and return the last tool_use name."""
    for rec in reversed(records):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    return name
    return None


def _extract_last_ts(records: list[dict], role: str) -> str | None:
    """Return the most recent ``timestamp`` field for records of the
    given ``type`` (``user`` or ``assistant``). Returns the raw ISO-8601
    string as written by Claude Code."""
    for rec in reversed(records):
        if rec.get("type") != role:
            continue
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def _has_completed_marker(records: list[dict]) -> bool:
    """Check whether the tail of the transcript contains a terminal
    ``task-notification ... status=completed`` marker (in any record's
    serialised body). Best-effort substring search on the JSON form.

    This is **state**, not classification — it just reports whether the
    marker was observed. Whether to map that to a status label is up
    to the caller.
    """
    for rec in records:
        try:
            blob = json.dumps(rec, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        if "task-notification" in blob and "completed" in blob:
            return True
    return False


# ─── public API ──────────────────────────────────────────────────────────────


def subagent_get_state(
    agent_id: str | None = None,
    project_path: str | None = None,
    session_id: str | None = None,
    *,
    projects_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Pure state data for Claude Code Agent-tool subagents.

    Walks ``~/.claude/projects/<project_hash>/<session>/subagents/agent-*.jsonl``
    and returns one dict per matching transcript. No classification —
    the caller (orochi, or any orchestrator) decides how to map these
    facts to a ``running`` / ``stale`` / ``dead`` / ``completed``
    label.

    Parameters
    ----------
    agent_id:
        If given, only return the subagent whose filename matches
        ``agent-<agent_id>.jsonl``. Otherwise scan all.
    project_path:
        Absolute filesystem path of the project (e.g. ``/home/.../proj/lead``).
        Defaults to ``os.getcwd()``.
    session_id:
        Claude Code session UUID. If ``None``, every session under the
        project is scanned.
    projects_root:
        Override the ``~/.claude/projects`` root (used by tests).

    Returns
    -------
    list[dict]
        One dict per subagent. Keys:

        * ``id`` — short subagent identifier (filename stem minus ``agent-``)
        * ``description`` — first user message (truncated to 500 chars)
        * ``jsonl_path`` — absolute path to the transcript file
        * ``size_bytes`` — current file size
        * ``mtime_iso`` — last-modified time as ISO-8601 (``...Z``)
        * ``mtime_epoch`` — last-modified time as float epoch seconds
        * ``last_tool`` — name of the most recent ``tool_use`` assistant block
        * ``last_assistant_ts_iso`` — timestamp of the most recent assistant record
        * ``last_user_ts_iso`` — timestamp of the most recent user record
        * ``session_id`` — Claude Code session UUID containing the subagent
        * ``project_hash`` — the ``-foo-bar-baz`` project hash
        * ``has_completed_marker`` — whether the tail contains a
          ``task-notification ... status=completed`` payload

        Returns ``[]`` if the project dir doesn't exist (e.g. on a CI
        runner with no ``~/.claude/``).
    """
    root = Path(projects_root) if projects_root is not None else _claude_projects_root()
    proj = project_path or os.getcwd()
    project_hash = _project_hash_for(proj)
    project_dir = root / project_hash
    if not project_dir.is_dir():
        return []

    if session_id is not None:
        session_dirs = [project_dir / session_id]
    else:
        try:
            session_dirs = [p for p in project_dir.iterdir() if p.is_dir()]
        except OSError:
            return []

    results: list[dict[str, Any]] = []
    for sess_dir in session_dirs:
        subagents_dir = sess_dir / "subagents"
        if not subagents_dir.is_dir():
            continue
        if agent_id is not None:
            candidates = [subagents_dir / f"agent-{agent_id}.jsonl"]
        else:
            try:
                candidates = sorted(subagents_dir.glob("agent-*.jsonl"))
            except OSError:
                continue
        for jsonl in candidates:
            if not jsonl.is_file():
                continue
            try:
                st = jsonl.stat()
            except OSError:
                continue
            tail_records = _iter_tail_records(jsonl)
            head_records = _iter_head_records(jsonl)
            stem = jsonl.stem  # "agent-<id>"
            sub_id = stem[len("agent-") :] if stem.startswith("agent-") else stem
            mtime_iso = (
                datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
            results.append(
                {
                    "id": sub_id,
                    "description": _extract_description(head_records),
                    "jsonl_path": str(jsonl),
                    "size_bytes": st.st_size,
                    "mtime_iso": mtime_iso,
                    "mtime_epoch": st.st_mtime,
                    "last_tool": _extract_last_tool(tail_records),
                    "last_assistant_ts_iso": _extract_last_ts(
                        tail_records, "assistant"
                    ),
                    "last_user_ts_iso": _extract_last_ts(tail_records, "user"),
                    "session_id": sess_dir.name,
                    "project_hash": project_hash,
                    "has_completed_marker": _has_completed_marker(tail_records),
                }
            )
    return results


# ─── MCP registration ────────────────────────────────────────────────────────


def register_subagent_tools(mcp) -> None:
    """Register the Claude Code subagent monitoring tools on ``mcp``."""
    mcp.tool()(subagent_get_state)
