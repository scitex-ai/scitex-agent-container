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

QUOTA_CACHE_HOST_PATH_DEFAULT = "/home/ywatanabe/.scitex/quota-cache.json"
QUOTA_CACHE_CONTAINER_PATH = "/var/sac/quota-cache.json"
QUOTA_CACHE_HOST_PATH_ENV = "SAC_QUOTA_CACHE_HOST_PATH"


def _resolve_quota_cache_host_path() -> Path:
    """The host path to bind, honouring the ``SAC_QUOTA_CACHE_HOST_PATH`` override."""
    override = os.environ.get(QUOTA_CACHE_HOST_PATH_ENV, "").strip()
    return Path(override) if override else Path(QUOTA_CACHE_HOST_PATH_DEFAULT)


__all__ = [
    "QUOTA_CACHE_CONTAINER_PATH",
    "QUOTA_CACHE_HOST_PATH_DEFAULT",
    "QUOTA_CACHE_HOST_PATH_ENV",
    "_resolve_quota_cache_host_path",
]
