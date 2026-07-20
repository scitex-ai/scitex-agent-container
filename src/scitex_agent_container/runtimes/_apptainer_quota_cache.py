"""Quota-cache host-path resolution for the agent container bind.

Extracted from :mod:`._apptainer_build_argv`, which sat exactly at the 512-line
cap. Resolving WHERE the host's quota cache lives is a lookup, not argv
assembly, so it is the cleanest seam in that module — and it was the only
reason ``_apptainer_build_argv`` imported ``os`` at all.

Both historical import paths keep resolving: ``_apptainer_build_argv``
re-exports these names (and ``_apptainer_runtime`` re-exports them from there),
so ``from runtimes._apptainer_runtime import QUOTA_CACHE_CONTAINER_PATH`` and
the ``_apptainer_build_argv`` spelling are both unchanged.

Quota-cache visibility (#16) — see the original docstring in
``_apptainer_runtime`` for the full motivation. Host cron refreshes the
canonical path every 10 min; the bind is read-only and conditional on the file
existing, so quota-cron-less hosts (CI, fresh installs) can still launch agents.
"""

from __future__ import annotations

import os
from pathlib import Path

# The canonical host source is sac's OWN runtime dir — SSOT with the writer's
# default (:data:`_account.quota_cache.HOST_RUNTIME_CACHE_SUBPATH`) and the
# reader's first candidate. Binding the legacy top-level path while the writer
# had migrated to runtime is what let the in-container reader read a stale file
# (2026-07-20 quota-blind incident); all three layers must agree on ONE path.
QUOTA_CACHE_HOST_PATH_DEFAULT = (
    "/home/ywatanabe/.scitex/agent-container/runtime/quota-cache.json"
)
QUOTA_CACHE_CONTAINER_PATH = "/var/sac/quota-cache.json"
QUOTA_CACHE_HOST_PATH_ENV = "SAC_QUOTA_CACHE_HOST_PATH"
# Retired write target (pre-2026-07 top-level path). Kept ONLY as a read-only
# bind FALLBACK for a host whose populator has not migrated to the runtime dir
# yet — a stale-but-present legacy cache is never worse than no bind at all.
QUOTA_CACHE_HOST_PATH_LEGACY = "/home/ywatanabe/.scitex/quota-cache.json"


def _resolve_quota_cache_host_path() -> Path:
    """The host path to bind, honouring the ``SAC_QUOTA_CACHE_HOST_PATH`` override.

    No override → the canonical runtime path when it exists, else the retired
    legacy top-level path when THAT exists (transitional), else the runtime
    path. A non-existent source makes the bind a conditional no-op, so the
    in-container reader degrades to an honest ``None`` rather than binding a
    ghost file — mirrors the reader's runtime-first / legacy-fallback order.
    """
    override = os.environ.get(QUOTA_CACHE_HOST_PATH_ENV, "").strip()
    if override:
        return Path(override)
    runtime = Path(QUOTA_CACHE_HOST_PATH_DEFAULT)
    if runtime.exists():
        return runtime
    legacy = Path(QUOTA_CACHE_HOST_PATH_LEGACY)
    if legacy.exists():
        return legacy
    return runtime


__all__ = [
    "QUOTA_CACHE_CONTAINER_PATH",
    "QUOTA_CACHE_HOST_PATH_DEFAULT",
    "QUOTA_CACHE_HOST_PATH_ENV",
    "QUOTA_CACHE_HOST_PATH_LEGACY",
    "_resolve_quota_cache_host_path",
]
