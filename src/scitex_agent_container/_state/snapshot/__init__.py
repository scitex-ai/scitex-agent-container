"""Self-snapshot subcommand (todo#286).

Thin re-export shim preserving the historical ``_state.snapshot``
public API. See ``_io.py`` for the gather/take/read surface, ``_diff.py``
for diff construction, ``_lock.py`` for the per-agent advisory lock,
``_paths.py`` for cache-dir resolution, and ``_sidecars.py`` for the
sidecar registry.

Kept deliberately stdlib-only: no psutil, no yaml, no new deps.
"""

from __future__ import annotations

from ._diff import _flatten, compute_diff_fields
from ._io import (
    _atomic_write_json,
    _now_iso,
    _probe_claude_pid,
    _probe_load1,
    _probe_mem,
    _probe_mem_darwin,
    _probe_mem_linux,
    _probe_nproc,
    _probe_screen_count,
    _probe_tmux,
    _probe_tmux_pids,
    _proc_count,
    _run,
    gather_snapshot,
    logger,
    read_latest,
    snapshot_tick,
    take_snapshot,
)
from ._lock import _snapshot_lock
from ._paths import (
    _diff_path,
    _latest_path,
    _lock_path,
    _prev_path,
    cache_dir,
)
from ._sidecars import (
    _AGENT_META_KEYS,
    _SIDECARS,
    SidecarInfo,
    _project_agent_meta,
    _sidecar_alive,
    _sidecars_payload,
    register_sidecar,
)

__all__ = [
    "SidecarInfo",
    "cache_dir",
    "compute_diff_fields",
    "gather_snapshot",
    "read_latest",
    "register_sidecar",
    "snapshot_tick",
    "take_snapshot",
]
