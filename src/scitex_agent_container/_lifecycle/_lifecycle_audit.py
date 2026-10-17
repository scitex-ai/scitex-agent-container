"""Local lifecycle invocation audit log.

Every local lifecycle entry point (``agent_start`` / ``agent_stop`` /
``agent_restart`` / dispatcher start-stop) records one JSONL row with
actor, timestamp, and flags. A restart that bypasses the listen API is
otherwise unattributable — this log answers "who cycled this agent,
when, with which flags" without trusting any single caller's own
reporting.

Best-effort by contract: a failed append is a logged warning, never a
lifecycle fault. The path honours ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
via :func:`._runtime_paths.runtime_base_dir`, so tests isolate with env.
"""

from __future__ import annotations

import getpass
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

import scitex_logging as slogging

log = slogging.getLogger(__name__)

AUDIT_FILENAME = "lifecycle-audit.jsonl"


def audit_log_path() -> Path:
    """JSONL audit path under the runtime base dir (read at call time)."""
    from .._runtime_paths import runtime_base_dir

    return runtime_base_dir() / "logs" / AUDIT_FILENAME


def resolve_audit_actor(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Attribution for one local invocation (never raises).

    ``parent_agent`` names the SAC agent whose container shelled out
    (``SAC_NAME``), else ``None`` for a bare CLI / lead / operator
    launch. ``user`` is the OS login; ``uid``/``pid`` pin the process.
    """
    env = environ if environ is not None else os.environ
    get = env.get if hasattr(env, "get") else (lambda k: None)
    try:
        user = get("USER") or get("LOGNAME") or getpass.getuser()
    except Exception:  # stx-allow: fallback (no login name available)
        user = None
    try:
        uid = os.getuid()
    except (AttributeError, OSError):
        uid = None
    return {
        "parent_agent": get("SAC_NAME")
        or get("SCITEX_AGENT_CONTAINER_NAME"),
        "caller": get("SAC_CALLER"),
        "user": user,
        "uid": uid,
        "pid": os.getpid(),
    }


def record_lifecycle_invocation(
    action: str,
    *,
    name: str | None = None,
    flags: Mapping[str, Any] | None = None,
    actor: Mapping[str, Any] | None = None,
    result: Any = None,
    log_path: Path | None = None,
) -> bool:
    """Append one audit row; True on write, False on any failure.

    Never raises — audit loss must not shadow the lifecycle result it
    describes. ``flags`` should carry the invocation's own options
    (force / fresh / session / engine / drain), never secrets.
    """
    try:
        entry = {
            "ts": time.time(),
            "action": str(action),
            "name": name,
            "actor": dict(actor) if actor is not None else (
                resolve_audit_actor()
            ),
            "flags": dict(flags or {}),
            "result": result,
        }
        path = log_path if log_path is not None else audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str))
            handle.write("\n")
        return True
    except Exception as exc:  # stx-allow: fallback (audit is best-effort)
        log.warning("lifecycle audit append failed (%s): %s", action, exc)
        return False


def audit(action: str, name: str | None = None, **flags: Any) -> None:
    """One-line audit entry for a lifecycle call site (never raises)."""
    try:
        record_lifecycle_invocation(action, name=name, flags=flags)
    except Exception:  # stx-allow: fallback (audit is best-effort)
        pass


__all__ = [
    "AUDIT_FILENAME",
    "audit",
    "audit_log_path",
    "record_lifecycle_invocation",
    "resolve_audit_actor",
]
