"""Per-agent SLURM state file handling.

Tracks ``job_id``, sbatch script path, and submission stdout in a JSON
file under ``$SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR`` (defaults to
``local_state.runtime_path("agent-container", "slurm-state")``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scitex_config._ecosystem import local_state

_STATE_DIR_ENV = "SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR"


def _state_dir() -> Path:
    default = local_state.runtime_path("agent-container", "slurm-state")
    return Path(os.environ.get(_STATE_DIR_ENV, str(default)))


def _state_path(name: str) -> Path:
    return _state_dir() / f"{name}.json"


def _write_state(name: str, data: dict) -> None:
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    _state_path(name).write_text(json.dumps(data, indent=2))


def _read_state(name: str) -> dict | None:
    p = _state_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clear_state(name: str) -> None:
    p = _state_path(name)
    if p.exists():
        p.unlink()


__all__ = [
    "_clear_state",
    "_read_state",
    "_state_dir",
    "_state_path",
    "_write_state",
]
