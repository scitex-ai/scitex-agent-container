"""Quota-cache bind + in-container path advertisement for agent containers.

Extracted verbatim from :mod:`_apptainer_build_argv` (issue #16; see the
module-level docstring in ``_apptainer_runtime`` for what the quota cache is
and why the telegrammer bridge wants it). Behaviour is unchanged — this is a
move, not a rewrite — and it joins the existing ``_apptainer_*`` family
alongside ``_apptainer_listen_env`` / ``_apptainer_secret_env``, which package
one env-wiring concern each.
"""

from __future__ import annotations

__all__ = ["quota_cache_flags"]


def quota_cache_flags() -> list[str]:
    """Bind the quota cache read-only and advertise its in-container path.

    Read-only bind plus ``CCT_QUOTA_CACHE_PATH`` so the telegrammer bridge's
    default-path lookup hits the bind without any per-agent spec change.
    Returns ``[]`` when the host has no quota cache file, which is the normal
    state on a freshly provisioned host.
    """
    from ._apptainer_quota_cache import (
        QUOTA_CACHE_CONTAINER_PATH,
        _resolve_quota_cache_host_path,
    )

    quota_src = _resolve_quota_cache_host_path()
    if not quota_src.is_file():
        return []
    return [
        "--bind",
        f"{quota_src}:{QUOTA_CACHE_CONTAINER_PATH}:ro",
        "--env",
        f"CCT_QUOTA_CACHE_PATH={QUOTA_CACHE_CONTAINER_PATH}",
    ]
