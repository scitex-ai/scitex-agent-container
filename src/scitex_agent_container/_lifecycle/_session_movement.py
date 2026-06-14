"""Surface session.jsonl movement next to per-agent status JSON.

Operator mandate (lead a2a 1781e82a, 2026-06-14): the lead's
kick-cycle needs an objective MOVEMENT signal embedded directly in
``sac agents status --json`` so the supervisor can read "is this
agent actually producing?" without scraping ``heartbeat.json`` /
``session.jsonl`` out of band.

The session.jsonl bytes + last write mtime are already written into
``heartbeat.json`` every beat (see ``_runners._heartbeat_fields``).
This module promotes those signals — plus the heartbeat ts itself —
to top-level fields on the per-agent status payload so consumers can
key off three stable names:

  * ``session_jsonl_bytes`` (int)           — current file size.
  * ``session_jsonl_last_write`` (ISO-8601) — mtime, RFC 3339 UTC.
  * ``heartbeat_at`` (ISO-8601)             — last heartbeat ts.

Missing data is reported as ``(0, "")`` / empty string — explicit,
not ``None``/``null`` — so JSON consumers can drop the key check
and treat zero-bytes / empty-iso as the canonical "no signal yet".

Why a dedicated helper (and not just splatting ``heartbeat.json``
keys)? The heartbeat record is the WRITER's view, formatted for
the heartbeat loop's own bookkeeping (delta bytes, seconds-since-
last-beat). The status JSON envelope is the READER's view —
operators and downstream watchers want the simplest possible
"how big and when last touched" pair, decoupled from heartbeat
cadence and from any future change in the heartbeat payload shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

__all__ = [
    "resolve_state_dir",
    "session_jsonl_movement",
    "heartbeat_iso",
    "status_movement_fields",
]


def resolve_state_dir(name: str) -> Path | None:
    """Resolve the on-disk state dir for ``name`` (or ``None`` if not found).

    Mirrors the resolution that ``_state._meta.transcript._read_sdk_session_state``
    uses on the read side: project-local scope first (matches
    ``runtimes.claude_session._project_runtime_root`` on the write
    side), then the default ``~/.scitex/agent-container/runtime/<name>``
    fallback.

    Returns ``None`` when neither candidate exists on disk so callers
    can collapse that to the all-defaults shape without an explicit
    is-dir check at every call site.
    """
    if not name:
        return None
    candidates: list[Path] = []
    try:
        from scitex_config._ecosystem import local_state

        scope = local_state.find_project_scope("agent-container")
    except Exception:  # stx-allow: fallback (reason: scitex-config is optional; degrade to home-scope state dir)
        scope = None
    if scope is not None:
        candidates.append(scope / "runtime" / name)
    try:
        from .._runners import claude_session as _runner

        candidates.append(_runner.state_dir_for(name))
    except Exception:  # stx-allow: fallback (reason: runner module import may fail in partial installs — home-scope candidate alone is enough)
        pass
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def session_jsonl_movement(state_dir: Path | str) -> Tuple[int, str]:
    """Return ``(bytes, last_write_iso)`` for ``<state_dir>/session.jsonl``.

    ``bytes`` is the current file size in bytes. ``last_write_iso`` is
    the file's mtime as an ISO-8601 UTC timestamp (``YYYY-MM-DDTHH:MM:SS+00:00``).

    Missing / unreadable file → ``(0, "")``. Explicit empty string —
    NOT ``None``/``null`` — so callers serialising into JSON don't
    have to special-case missing-data per field.

    Best-effort: every IO failure (permission-denied, race with
    rotate, etc.) degrades to ``(0, "")`` so the status command
    never crashes on a movement-probe hiccup.
    """
    if not state_dir:
        return 0, ""
    path = Path(state_dir) / "session.jsonl"
    try:
        st = path.stat()
    except OSError:
        return 0, ""
    size = int(getattr(st, "st_size", 0) or 0)
    try:
        iso = (
            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            if st.st_mtime
            else ""
        )
    except (OSError, OverflowError, ValueError):
        iso = ""
    return size, iso


def heartbeat_iso(state_dir: Path | str) -> str:
    """Return the last heartbeat ts as ISO-8601, or ``""`` if absent.

    Reads ``<state_dir>/heartbeat.json`` and converts the ``ts``
    (unix seconds, as written by ``_session_state.write_heartbeat``)
    to an RFC-3339 UTC string. Best-effort: any read or decode
    failure degrades to the empty string so the status command
    never crashes on a heartbeat-probe hiccup.

    Returns an empty string (NOT ``None``) so JSON consumers can
    treat the field as "always present, never null".
    """
    if not state_dir:
        return ""
    path = Path(state_dir) / "heartbeat.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text:
        return ""
    import json as _json

    try:
        payload = _json.loads(text)
    except (ValueError, TypeError):
        return ""
    ts = payload.get("ts") if isinstance(payload, dict) else None
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def status_movement_fields(state_dir: Path | str | None) -> dict:
    """Build the three-key status JSON envelope additive block.

    Returns a dict with ``session_jsonl_bytes``, ``session_jsonl_last_write``,
    and ``heartbeat_at`` populated from the agent's state dir. All
    three keys are always present — missing data renders as ``0`` or
    ``""`` so the schema stays stable across the registered / not-yet-
    started / running transitions.

    ``state_dir=None`` (no resolvable state dir, e.g. cross-host
    instance) yields the all-defaults shape — same three keys with
    the explicit empty values.
    """
    bytes_, last_write = session_jsonl_movement(state_dir or "")
    return {
        "session_jsonl_bytes": int(bytes_),
        "session_jsonl_last_write": last_write,
        "heartbeat_at": heartbeat_iso(state_dir or ""),
    }
