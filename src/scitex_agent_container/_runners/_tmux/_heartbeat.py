"""tmux runner — heartbeat derived from ``tmux capture-pane``.

Day-2 (C, opt-in) — emits a ``heartbeat.json`` shape compatible with
the SDK runner so the existing dashboard / ``sac fleet status`` reads
it without runtime-awareness.

The tmux driver has no SDK quota stream, so token-count fields are
``null`` (best-effort, marked as "not probed" — not "0"). The state
field is derived from a pane-classifier applied to the recent capture:

* ``working`` — pane shows ``Working…`` / ``Ruminating…`` markers
* ``idle``    — pane shows the ready marker (``❯ … bypass permissions``)
* ``starting`` — pane is empty / pre-login
* ``unknown`` — capture failed / pane has none of the above

The poller is a thin loop callers wire into their lifecycle thread;
the file is materialised atomically via ``tmp + replace`` exactly
like ``_session_state.write_heartbeat`` does.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# Mirrors the SDK runner's state vocabulary so a generic reader doesn't
# have to branch on runtime kind.
STATE_STARTING = "starting"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_UNKNOWN = "unknown"


class PaneSource(Protocol):
    """Minimal seam so the poller is testable without real tmux."""

    def capture_pane(self, session: str) -> str: ...


def classify_pane_state(content: str) -> str:
    """Return the heartbeat ``state`` for the given pane capture.

    Best-effort, last-5-lines window. The rules:

      * empty pane → starting (the multiplexer just came up)
      * ``Working…`` / ``Ruminating…`` in tail → working
      * ``bypass permissions`` ready marker → idle
      * none of the above → unknown
    """
    if not content or not content.strip():
        return STATE_STARTING
    tail = "\n".join(ln for ln in content.splitlines() if ln.strip()).splitlines()[-5:]
    tail_text = "\n".join(tail)
    if "Working…" in tail_text or "Ruminating…" in tail_text:
        return STATE_WORKING
    if "bypass permissions" in tail_text and "Enter to confirm" not in tail_text:
        return STATE_IDLE
    return STATE_UNKNOWN


def _tmp_pressure_fields(probe_path: str = "/tmp") -> dict:
    """Mirror ``_session_state._tmp_pressure_fields`` shape.

    Returns ``{tmp_used_pct}`` when probeable; empty dict otherwise so
    the field is ABSENT rather than misleadingly 0.
    """
    try:
        usage = shutil.disk_usage(probe_path)
    except OSError:
        return {}
    if usage.total <= 0:
        return {}
    return {"tmp_used_pct": round(usage.used / usage.total * 100.0, 1)}


def build_heartbeat_payload(
    *,
    pid: int,
    pane_content: str,
    started_at: float | None = None,
    now: float | None = None,
) -> dict:
    """Build one ``heartbeat.json`` payload from a pane capture.

    Shape matches the SDK runner's heartbeat so a generic reader (the
    dashboard, ``sac fleet status``) doesn't have to branch on
    runtime kind. Token fields are ``null`` — the tmux driver has no
    quota stream, and marking them ``null`` rather than ``0`` lets
    the reader distinguish "not probed" from "empty quota".
    """
    ts = time.time() if now is None else now
    payload: dict = {
        "ts": ts,
        "pid": pid,
        "state": classify_pane_state(pane_content),
        "runtime": "tmux",
        # Quota fields are tracked by the SDK only; the tmux driver
        # has no introspection into per-turn token usage. Surface them
        # explicitly as null so a reader can show "not available".
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    if started_at is not None:
        payload["started_at"] = started_at
        payload["elapsed_s"] = round(max(0.0, ts - started_at), 3)
    payload.update(_tmp_pressure_fields())
    return payload


def write_heartbeat(state_dir: Path, payload: dict) -> Path:
    """Atomic write of ``heartbeat.json`` (tmp + ``Path.replace``).

    Returns the final path so callers can log / surface it.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    final = state_dir / "heartbeat.json"
    tmp = state_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(final)
    return final


def poll_once(
    *,
    pane_source: PaneSource,
    session: str,
    state_dir: Path,
    pid: int,
    started_at: float | None = None,
) -> dict:
    """Do one capture → classify → write cycle. Returns the payload.

    Callers wrap this in a loop sized to the agent's heartbeat tick.
    Pure best-effort: any failure inside is logged and a degraded
    payload is written (so a missing heartbeat means the runner is
    dead, not just unlucky on one tick).
    """
    try:
        content = pane_source.capture_pane(session)
    except Exception:  # stx-allow: fallback (capture is optional)
        logger.exception("tmux heartbeat capture failed for %s", session)
        content = ""
    payload = build_heartbeat_payload(
        pid=pid, pane_content=content, started_at=started_at
    )
    try:
        write_heartbeat(state_dir, payload)
    except OSError:
        logger.exception("tmux heartbeat write failed for %s in %s", session, state_dir)
    return payload


__all__ = [
    "STATE_IDLE",
    "STATE_STARTING",
    "STATE_UNKNOWN",
    "STATE_WORKING",
    "PaneSource",
    "build_heartbeat_payload",
    "classify_pane_state",
    "poll_once",
    "write_heartbeat",
]
