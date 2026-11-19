"""The ``host_exec`` audit log — one JSONL line per invocation or refusal.

Extracted from :mod:`._host_exec` (over the per-file line cap); names are
re-exported there, so ``_host_exec._append_audit`` etc. keep resolving.

The operator accepted that this log records without preventing (2026-07-01
Q1a/Q2a): it is the forensic trail. Each line carries ``identity`` — how the
caller name was established (``agent-token`` / ``node-bearer`` /
``name-claimed (legacy, unauthenticated)``) — so unauthenticated calls stay
countable while the fleet migrates onto per-agent tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .._logging import get_logger

# Path is fixed (matches the other runtime logs). Test seam: the handler's
# ``audit_writer`` keyword.
_AUDIT_LOG_PATH = (
    Path.home() / ".scitex" / "agent-container" / "runtime" / "logs" / "host_exec.log"
)


def _audit_log_path() -> Path:
    return _AUDIT_LOG_PATH


def _append_audit(entry: dict[str, Any]) -> None:
    """Append one JSONL line. Best-effort — a failed audit log MUST NOT break
    the response (the exec already ran; log the miss to stderr and continue)."""
    path = _audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # stx-allow: fallback (best-effort audit; must not shadow the real exec result)
        # Log so the miss is visible in the listen journal, then continue —
        # the exec's real result still returns to the caller. Routed through
        # scitex-logging rather than a raw stderr print: this is the sole
        # account of a LOST AUDIT RECORD, so it must carry its origin and
        # survive in the runtime log rather than only in whatever journal
        # happened to be capturing the daemon's stderr.
        get_logger(__name__).error(f"audit log append failed at {path}: {exc}")


__all__ = ["_AUDIT_LOG_PATH", "_append_audit", "_audit_log_path"]
