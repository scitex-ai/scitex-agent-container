"""Snapshot cache paths (per-agent latest/prev/diff/lock).

Per local-state §4b: snapshots live under ``runtime/``. ``$SAC_CACHE_DIR``
/ ``$SCITEX_AGENT_CONTAINER_CACHE_DIR`` overrides everything; otherwise
resolves via the SciTeX local-state cascade (project-scope
``<repo>/.scitex/agent-container/runtime/cache/`` wins, falls back to
``$SCITEX_DIR/agent-container/runtime/cache/``).
"""

from __future__ import annotations

from pathlib import Path

from ..._env import getenv as _sac_env


def _ensure_cache_dir(path: Path) -> Path:
    """Create the private cache directory, including on first status read.

    ``mkdir(..., exist_ok=True)`` makes concurrent first-use calls safe.  The
    explicit chmod is intentional: ``mode`` only applies when this call wins
    creation, so it would otherwise leave a pre-existing or concurrently
    created directory with permissions inherited from an unsafe umask.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def cache_dir() -> Path:
    """Per-agent snapshot cache (under `runtime/` per local-state §4b).

    ``$SAC_CACHE_DIR`` / ``$SCITEX_AGENT_CONTAINER_CACHE_DIR`` override
    everything; otherwise resolves via the SciTeX local-state cascade
    (project-scope `<repo>/.scitex/agent-container/runtime/cache/` wins,
    falls back to `$SCITEX_DIR/agent-container/runtime/cache/`).
    """
    override = _sac_env("CACHE_DIR")
    if override:
        return _ensure_cache_dir(Path(override).expanduser())
    from scitex_config._ecosystem import local_state as _local_state

    return _ensure_cache_dir(_local_state.runtime_path("agent-container", "cache"))


def _latest_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.latest.json"


def _prev_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.prev.json"


def _diff_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.diff.json"


def _lock_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.lock"
