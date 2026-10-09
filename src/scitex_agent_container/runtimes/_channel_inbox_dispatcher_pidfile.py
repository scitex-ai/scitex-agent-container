"""Version-stamped pidfile codec for the channel inbox dispatcher.

Split from :mod:`._channel_inbox_dispatcher_ownership` (per-file
512-line cap): this module owns the pidfile bytes — stamped JSON in
this generation, bare ``"<pid>\\\\n"`` before it — while the ownership
module owns the decision to signal. Readers accept both generations:
a prior-version owner is accepted with a warning, never refused, so a
rolling upgrade cannot strand a running dispatcher.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ._inbox_sidecar_reconcile import CURRENT_MODULE

#: Pidfiles written before version-stamping carry a bare ``"<pid>\\\\n"``.
#: Anything carrying ``sac_version`` was written by this generation.
PIDFILE_SCHEMA_VERSION = 1

#: A pidfile whose mtime is older than this is flagged ``expired`` in
#: the stop outcome. Expiry alone never authorises a signal — it only
#: explains why a pointer was reaped — because an idle owner with a
#: stalled heartbeat is still the owner when its argv proves it.
PIDFILE_TTL_S = 600.0


@dataclass(frozen=True)
class DispatcherPidRecord:
    """One parsed ``channel-inbox-dispatcher.pid`` file."""

    pid: int
    sac_version: str | None
    module: str | None
    incarnation_id: str | None
    legacy: bool


def _sac_version() -> str:
    """Best-effort SAC version for the pidfile stamp (never raises)."""
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # stx-allow: fallback (unstamped pidfile stays usable)
        return "unknown"


def read_dispatcher_pidfile(path: Path) -> DispatcherPidRecord | None:
    """Parse a pidfile in either generation's format (never raises).

    Stamped JSON (this generation) carries pid + version + module +
    incarnation id. A bare integer (prior generations) parses as a
    legacy record with ``sac_version=None`` — accepted with a warning,
    never refused.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("pid"), int):
        return DispatcherPidRecord(
            pid=int(payload["pid"]),
            sac_version=payload.get("sac_version"),
            module=payload.get("module"),
            incarnation_id=payload.get("incarnation_id"),
            legacy=False,
        )
    try:
        return DispatcherPidRecord(
            pid=int(text.split()[0]),
            sac_version=None,
            module=None,
            incarnation_id=None,
            legacy=True,
        )
    except (ValueError, IndexError):
        return None


def write_dispatcher_pidfile(
    path: Path,
    pid: int,
    *,
    incarnation_id: str | None,
    module: str = CURRENT_MODULE,
) -> None:
    """Stamp a versioned pidfile (lets the next generation accept us)."""
    payload = {
        "schema": PIDFILE_SCHEMA_VERSION,
        "pid": int(pid),
        "sac_version": _sac_version(),
        "module": module,
        "incarnation_id": incarnation_id,
        "written_at": time.time(),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(0o600)


def touch_dispatcher_pidfile(path: Path) -> bool:
    """Heartbeat one mtime tick on a pidfile the owner still holds.

    Called by the owning dispatcher process only — never by a stop
    path — so a fresh mtime is proof of a live owner. Never raises.
    """
    try:
        os.utime(path, None)
        return True
    except OSError:
        return False


def pidfile_expired(path: Path, ttl_s: float = PIDFILE_TTL_S) -> bool:
    """Whether a pidfile's mtime is older than ``ttl_s`` (never raises)."""
    try:
        return (time.time() - path.stat().st_mtime) > ttl_s
    except OSError:
        return False


__all__ = [
    "PIDFILE_SCHEMA_VERSION",
    "PIDFILE_TTL_S",
    "DispatcherPidRecord",
    "pidfile_expired",
    "read_dispatcher_pidfile",
    "touch_dispatcher_pidfile",
    "write_dispatcher_pidfile",
]
