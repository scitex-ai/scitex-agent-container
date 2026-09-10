"""Session-log tail reading + redaction for the detail view.

The log is read from the listener's committed public endpoint
``GET /agents/<name>/tail?follow=false`` (an SSE stream of ``{"line_no",
"record"}``), NOT by scraping the runtime directory — so the dashboard keeps
delegating to the listener and opens no second read path. Records are
SUMMARIZED (one line each) and REDACTED before reaching the browser, because a
raw Claude/Codex session transcript carries tool output, file contents, and
secrets.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Bounded by design: the detail view shows a tail, not the whole transcript.
DEFAULT_TAIL = 25

# Secret patterns scrubbed from any record text before it is rendered.
_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.=]{16,}"
    r"|(?:api[_-]?key|token|password|passwd|secret)\s*[=:]\s*[\"']?[A-Za-z0-9_\-\.=]{8,}"
    r"|[A-Za-z0-9+/]{40,}={0,2}"  # long base64 blobs (keys, hashes-as-secrets)
    r")",
    re.IGNORECASE,
)


def redact(text: Any) -> str:
    """Scrub obvious secrets from a string. Never raises on odd input."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    return _SECRET_RE.sub("[REDACTED]", text)


def _compact(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(value)
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def summarize_record(record: Any) -> str:
    """A single, secret-free one-line summary of a session record."""
    if not isinstance(record, dict):
        return redact(_compact(record))
    # A2A / harness records vary; pull the most informative fields present.
    type_ = record.get("type") or record.get("kind") or record.get("event") or "record"
    body = ""
    for key in ("text", "content", "message", "summary", "title", "command", "query"):
        if key in record and record[key] is not None:
            body = _compact(record[key])
            break
    if not body:
        # Fall back to the first non-trivial scalar, skipping the meta keys.
        for k, v in record.items():
            if k in {"type", "kind", "event", "ts", "timestamp", "line_no", "id", "uuid"}:
                continue
            if isinstance(v, (str, int, float, bool)) and str(v).strip():
                body = _compact(v)
                break
    line = f"{type_}: {body}" if body else str(type_)
    return redact(line)


def parse_tail_frames(body: str, limit: int = DEFAULT_TAIL) -> list[str]:
    """Parse an SSE tail body into the last ``limit`` summarized lines.

    Each data frame is ``data: {"line_no": N, "record": {...}}``. Keeps only
    well-formed frames; a missing ``record`` yields a raw-line summary.
    """
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        try:
            frame = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            continue
        record = frame.get("record") if isinstance(frame, dict) else None
        lines.append(summarize_record(record))
    return lines[-limit:]


__all__ = [
    "DEFAULT_TAIL",
    "parse_tail_frames",
    "redact",
    "summarize_record",
]
