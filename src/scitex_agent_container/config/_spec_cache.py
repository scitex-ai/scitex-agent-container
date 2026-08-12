#!/usr/bin/env python3
"""Parsed-spec cache, so `sac agents list` stops re-parsing 100+ YAML files.

WHY THIS EXISTS. `sac agents list` took ~9.4s on 89 definitions, and profiling
put 4.5s of that in `load_config` -> `yaml.safe_load`, once per spec. The
operator wants `watch -n 10 sac agents list` to be usable, and every `watch`
tick is a FRESH PROCESS, so an in-process memo buys nothing. The cache has to
outlive the process.

CORRECTNESS BEFORE SPEED. A spec cache that serves stale content is far worse
than a slow list: the whole point of the command is to tell you what the fleet
IS. So the key is (size, mtime_ns) of the spec file, both of which change on any
ordinary edit, and any doubt whatsoever -- unreadable cache, pickle error,
version mismatch, missing key -- falls through to a real parse. The cache can
only ever make the command faster, never make it answer differently.

WHERE IT LIVES. `$SCITEX_DIR/agent-container/runtime/` per the ecosystem
local-state rule (scitex-dev _skills 01_ecosystem/12_local-state-resolution.md):
RUNTIME is per-host, regenerable and never tracked. Deleting this file costs one
slow run.
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any

# Bump when the cached VALUE shape changes. A mismatch is a miss, not an error.
_CACHE_VERSION = 1
_ENV_DISABLE = "SAC_SPEC_CACHE_DISABLE"


def _cache_path() -> Path | None:
    """Cache file under the runtime/ dir, or None if it cannot be placed."""
    # stx-allow: fallback (reason: a cache that cannot be located must degrade
    # to "no cache", never break config loading)
    try:
        root = os.environ.get("SCITEX_DIR") or (Path.home() / ".scitex")
        d = Path(root).expanduser() / "agent-container" / "runtime"
        d.mkdir(parents=True, exist_ok=True)
        return d / "spec-parse-cache.pkl"
    except Exception:
        return None


def _stat_key(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) — changes on any ordinary edit."""
    # stx-allow: fallback (reason: an unstattable spec is simply not cacheable)
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except Exception:
        return None


def _load_all() -> dict:
    # stx-allow: fallback (reason: a corrupt/absent cache is a MISS, not a fault)
    try:
        p = _cache_path()
        if p is None or not p.exists():
            return {}
        with open(p, "rb") as f:
            blob = pickle.load(f)
        if not isinstance(blob, dict) or blob.get("v") != _CACHE_VERSION:
            return {}
        entries = blob.get("entries")
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}


def _store_all(entries: dict) -> None:
    # stx-allow: fallback (reason: failing to PERSIST a cache must never fail
    # the command that produced it — worst case the next run is slow again)
    try:
        p = _cache_path()
        if p is None:
            return
        tmp = p.with_suffix(".pkl.tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"v": _CACHE_VERSION, "entries": entries}, f, protocol=4)
        os.replace(tmp, p)  # atomic: a reader never sees a half-written cache
    except Exception:
        pass


_MEM: dict[str, Any] = {}
_DIRTY = False


def get(path: Path) -> Any | None:
    """Cached raw YAML blob for *path*, or None on any miss."""
    if os.environ.get(_ENV_DISABLE):
        return None
    key = _stat_key(path)
    if key is None:
        return None
    if not _MEM:
        _MEM.update(_load_all())
    hit = _MEM.get(str(path))
    if not isinstance(hit, tuple) or len(hit) != 2:
        return None
    stored_key, blob = hit
    # The stat key is the ENTIRE validity argument. Same size AND same
    # mtime_ns means the bytes we parsed are the bytes on disk now.
    return blob if stored_key == key else None


def put(path: Path, blob: Any) -> None:
    """Remember *blob* as the parse of *path*, keyed by its current stat."""
    global _DIRTY
    if os.environ.get(_ENV_DISABLE):
        return
    key = _stat_key(path)
    if key is None:
        return
    if not _MEM:
        _MEM.update(_load_all())
    _MEM[str(path)] = (key, blob)
    _DIRTY = True


def flush() -> None:
    """Persist if anything changed. Safe to call repeatedly."""
    global _DIRTY
    if _DIRTY and not os.environ.get(_ENV_DISABLE):
        _store_all(_MEM)
        _DIRTY = False
