"""Host process environment for the per-agent TUI turn bridge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from ._board_identity_env import raw_args_env
from ._fleet_env import effective_env

PGPASSFILE = "PGPASSFILE"
PGUSER = "PGUSER"
_STORE_ENV = ("SCITEX_CARDS_DB", "SCITEX_STORE_DSN", PGUSER)
_BOUNDED_CONNECT_SECONDS = "5"
_BOUNDED_PGOPTIONS = "-c statement_timeout=5000 -c lock_timeout=5000"


def turn_bridge_process_env(
    config: Any,
    *,
    host_environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a host-usable, project-scoped environment for one bridge.

    The SAC command process has the fleet-wide ``<owner>__cli`` identity.
    Inheriting it made a bridge authenticate as the caller rather than the
    agent.  Re-resolving through :func:`effective_env` supplies the same
    project role as the container launch without treating the caller's
    inherited ``PGUSER`` as a declaration.

    Only store-routing variables cross from the container launch model. Host
    process variables such as ``HOME``, ``PATH`` and ``TMPDIR`` remain the
    host's values. ``PGPASSFILE`` is always host-scoped: a spec/raw path names
    the container filesystem and must never be handed to this host process.
    """
    source = os.environ if host_environ is None else host_environ
    result = {str(key): str(value) for key, value in source.items()}
    resolved = effective_env(config)
    apptainer = getattr(config, "apptainer", None)
    raw = raw_args_env(
        getattr(apptainer, "raw_args", None) if apptainer is not None else None
    )
    resolved.update(raw)
    for key in _STORE_ENV:
        if key in resolved:
            result[key] = str(resolved[key])

    host_passfile = str(source.get(PGPASSFILE, "")).strip()
    host_home = str(source.get("HOME", "")).strip() or str(Path.home())
    result[PGPASSFILE] = host_passfile or str(Path(host_home) / ".pgpass")

    # libpq connection establishment and server-side waits are bounded even
    # when the operator did not provide stricter values.  HTTP acceptance has
    # its own wall-clock bound; these defaults also prevent abandoned backend
    # work from surviving far beyond that response.
    result["PGCONNECT_TIMEOUT"] = _BOUNDED_CONNECT_SECONDS
    result["PGOPTIONS"] = _BOUNDED_PGOPTIONS
    return result


def turn_bridge_db_observation(
    config: Any,
    process_env: Mapping[str, str],
    *,
    host_environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Describe bridge database identity sources without credential values."""
    source = os.environ if host_environ is None else host_environ
    spec_env = getattr(config, "env", None)
    spec_env = spec_env if isinstance(spec_env, Mapping) else {}
    apptainer = getattr(config, "apptainer", None)
    raw = raw_args_env(
        getattr(apptainer, "raw_args", None) if apptainer is not None else None
    )
    configured_pguser = raw.get(PGUSER) or spec_env.get(PGUSER)
    ignored_container_passfile = PGPASSFILE in raw or PGPASSFILE in spec_env
    if str(source.get(PGPASSFILE, "")).strip():
        passfile_source = "host"
    else:
        passfile_source = "host_default"
    return {
        "configured_pguser": (
            str(configured_pguser) if configured_pguser is not None else None
        ),
        "effective_pguser": str(process_env.get(PGUSER, "")) or None,
        "pgpassfile_source": passfile_source,
        "container_pgpassfile_ignored": str(ignored_container_passfile).lower(),
    }


__all__ = ["turn_bridge_db_observation", "turn_bridge_process_env"]
