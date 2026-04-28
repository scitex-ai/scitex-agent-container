"""Recall context from a Claude Code session jsonl.

Used by ``sac recall <jsonl>`` to summarize a previous session — typically
the dead transcript left behind after a host crash — so a fresh agent
can re-read the salient state without paying the cost of a full
``--continue``.

Three things the CLI should support:

1. Path-in: any ``~/.claude/projects/<encoded>/<uuid>.jsonl``.
2. Stats: rough message/tool/time histogram so the user sees the shape
   before reading line-by-line.
3. Filtering: time window (``--last 8h``), role, substring match. Time
   filtering is the core use case — "what was I doing in the last
   8 hours of the dead session" is the reason this exists.

Kept dependency-free (stdlib only) so it loads even when the broader
sac runtime can't import (cf. the a2a / scitex_config gaps that block
the rest of the CLI).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator


# Records we treat as "conversational" rather than infrastructure noise.
# attachment / queue-operation / permission-mode / file-history-snapshot
# bloat the transcript without carrying user-facing intent.
ROLE_TYPES = ("user", "assistant", "system")


@dataclass
class Entry:
    """A flattened jsonl line with the bits we actually print."""

    ts: datetime | None
    type: str  # user | assistant | system | ... (raw jsonl 'type')
    role: str  # user | assistant | system | other
    text: str = ""  # joined text parts
    tool_uses: list[tuple[str, dict]] = field(default_factory=list)  # (name, input)
    is_tool_result: bool = False  # True if this 'user' record is actually a tool_result
    raw: dict = field(default_factory=dict)


@dataclass
class Stats:
    total_lines: int = 0
    parse_errors: int = 0
    by_type: Counter = field(default_factory=Counter)
    tool_uses: Counter = field(default_factory=Counter)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    session_id: str = ""
    cwd: str = ""
    version: str = ""

    @property
    def duration(self) -> timedelta | None:
        if self.first_ts and self.last_ts:
            return self.last_ts - self.first_ts
        return None


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)


def parse_duration(spec: str) -> timedelta:
    """Parse '8h', '30m', '1.5d', '2w', '45s' into a timedelta.

    Raises ValueError on unrecognised input. Whitespace is tolerated.
    """
    s = (spec or "").strip()
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"unrecognised duration: {spec!r}")
    value = float(m.group(1))
    unit = m.group(2).lower()
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return timedelta(seconds=value * factor)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Claude Code timestamps are ISO-8601 with a trailing 'Z'.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_text(content) -> tuple[str, list[tuple[str, dict]], bool]:
    """Pull joined text + tool_use pairs out of a message.content blob.

    Returns (joined_text, tool_uses, is_tool_result). The third element
    is True iff every part in the content list is a tool_result — the
    common shape for synthetic 'user' records that are really tool
    feedback, not human prompts.
    """
    if content is None:
        return "", [], False
    if isinstance(content, str):
        return content, [], False
    if not isinstance(content, list):
        return "", [], False
    texts: list[str] = []
    tools: list[tuple[str, dict]] = []
    saw_non_tool_result = False
    saw_tool_result = False
    for part in content:
        if not isinstance(part, dict):
            saw_non_tool_result = True
            continue
        ptype = part.get("type")
        if ptype == "text":
            saw_non_tool_result = True
            t = part.get("text") or ""
            if t:
                texts.append(t)
        elif ptype == "thinking":
            saw_non_tool_result = True
            t = part.get("thinking") or part.get("text") or ""
            if t:
                texts.append(f"[thinking] {t}")
        elif ptype == "tool_use":
            saw_non_tool_result = True
            tools.append((part.get("name") or "?", part.get("input") or {}))
        elif ptype == "tool_result":
            saw_tool_result = True
            t = part.get("content") or ""
            if isinstance(t, list):
                t = "\n".join(
                    (p.get("text") or "")
                    for p in t
                    if isinstance(p, dict)
                )
            if isinstance(t, str) and t:
                texts.append(f"[tool_result] {t}")
        else:
            saw_non_tool_result = True
    is_tool_result = saw_tool_result and not saw_non_tool_result
    return "\n".join(texts), tools, is_tool_result


def iter_entries(jsonl_path: Path | str) -> Iterator[Entry]:
    """Stream Entry records from a jsonl file. Skips infra-only lines."""
    path = Path(jsonl_path).expanduser()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = d.get("type", "")
            ts = _parse_ts(d.get("timestamp"))
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = msg.get("role") or rtype
            text, tools, is_tool_result = _extract_text(msg.get("content"))
            yield Entry(
                ts=ts,
                type=rtype,
                role=role or "",
                text=text,
                tool_uses=tools,
                is_tool_result=is_tool_result,
                raw=d,
            )


def collect_stats(jsonl_path: Path | str) -> Stats:
    """Single pass over the jsonl producing a histogram + time range."""
    s = Stats()
    path = Path(jsonl_path).expanduser()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            s.total_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                s.parse_errors += 1
                continue
            s.by_type[d.get("type", "?")] += 1
            ts = _parse_ts(d.get("timestamp"))
            if ts:
                if s.first_ts is None or ts < s.first_ts:
                    s.first_ts = ts
                if s.last_ts is None or ts > s.last_ts:
                    s.last_ts = ts
            if d.get("type") == "assistant":
                msg = d.get("message") or {}
                for part in msg.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        s.tool_uses[part.get("name") or "?"] += 1
            if not s.session_id:
                s.session_id = d.get("sessionId") or ""
            if not s.cwd:
                s.cwd = d.get("cwd") or ""
            if not s.version:
                s.version = d.get("version") or ""
    return s


def filter_entries(
    entries: Iterable[Entry],
    *,
    last: timedelta | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    role: str | None = None,
    contains: str | None = None,
    include_thinking: bool = False,
    include_tool_results: bool = True,
    skip_empty: bool = True,
    reference_now: datetime | None = None,
) -> Iterator[Entry]:
    """Apply CLI-style filters lazily.

    ``last`` is interpreted relative to ``reference_now`` (default: the
    last entry in the stream — but the caller usually passes the
    transcript's last_ts so 'last 8h' is "the final 8h of the
    transcript", not 'the last 8h before wallclock now'. The dead
    transcript may be days old).
    """
    items = list(entries) if reference_now is None else None
    if items is not None:
        # Need two passes only if last is specified and reference_now wasn't given.
        if last is not None and reference_now is None:
            ref_candidates = [e.ts for e in items if e.ts is not None]
            reference_now = max(ref_candidates) if ref_candidates else None
        source: Iterable[Entry] = items
    else:
        source = entries

    contains_lc = contains.lower() if contains else None

    for e in source:
        if role and (e.role or "") != role and role != "all":
            continue
        if not include_tool_results and e.is_tool_result:
            continue
        if not include_thinking and e.text.startswith("[thinking] "):
            continue
        if skip_empty and not e.text and not e.tool_uses:
            continue
        if last is not None and reference_now is not None and e.ts is not None:
            if (reference_now - e.ts) > last:
                continue
        if since is not None and (e.ts is None or e.ts < since):
            continue
        if until is not None and (e.ts is None or e.ts > until):
            continue
        if contains_lc and contains_lc not in e.text.lower():
            continue
        yield e


def format_stats(s: Stats) -> str:
    """Compact human-readable stats block."""
    lines = []
    lines.append(f"session_id : {s.session_id or '-'}")
    lines.append(f"cwd        : {s.cwd or '-'}")
    lines.append(f"version    : {s.version or '-'}")
    lines.append(f"lines      : {s.total_lines} (parse-errors: {s.parse_errors})")
    if s.first_ts and s.last_ts:
        lines.append(f"time range : {s.first_ts.isoformat()} → {s.last_ts.isoformat()}")
        dur = s.duration
        if dur:
            total_min = dur.total_seconds() / 60
            lines.append(f"duration   : {total_min:.1f} min")
    if s.by_type:
        lines.append("by_type    :")
        for t, n in s.by_type.most_common():
            lines.append(f"  {t}: {n}")
    if s.tool_uses:
        lines.append("tool_uses  :")
        for t, n in s.tool_uses.most_common():
            lines.append(f"  {t}: {n}")
    return "\n".join(lines)


def format_entry(e: Entry, *, body_limit: int | None = 600) -> str:
    """Single-message markdown-ish block."""
    ts = e.ts.isoformat() if e.ts else "-"
    head = f"## [{ts}] {e.role or e.type}"
    body = e.text or ""
    if body_limit is not None and len(body) > body_limit:
        body = body[:body_limit].rstrip() + f" … (+{len(e.text) - body_limit} chars)"
    parts = [head]
    if body:
        parts.append(body)
    if e.tool_uses:
        tu_lines = [f"  - {name}({', '.join(sorted((inp or {}).keys()))})" for name, inp in e.tool_uses]
        parts.append("tool_use:\n" + "\n".join(tu_lines))
    return "\n".join(parts)
