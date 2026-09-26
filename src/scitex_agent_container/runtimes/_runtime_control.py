"""Small persistent vocabulary for runtime turn-admission control.

The state is deliberately harness-neutral.  Runtime adapters own detection and
recovery; status consumers only need to know whether a live session can admit a
turn, and why it cannot.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONTROL_STATE_FILENAME = "runtime-control.json"
READY = "ready"
STALE_LATCHED = "stale_latched"
RECOVERING = "recovering"


def control_state_path(state_dir: Path) -> Path:
    return state_dir / CONTROL_STATE_FILENAME


def read_control_state(state_dir: Path) -> dict[str, Any] | None:
    """Read one adapter observation; malformed/absent state is unknown."""
    try:
        value = json.loads(control_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_control_state(state_dir: Path, value: dict[str, Any]) -> None:
    """Atomically replace only the runtime-control marker."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = control_state_path(state_dir)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=state_dir)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "CONTROL_STATE_FILENAME",
    "READY",
    "RECOVERING",
    "STALE_LATCHED",
    "control_state_path",
    "read_control_state",
    "write_control_state",
]
