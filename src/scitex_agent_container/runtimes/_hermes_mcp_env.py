"""Hermes MCP entry env: launch-env refill and PostgreSQL credential checks.

Split out of :mod:`._hermes_profile` (which re-exports these names). Every
entry honours its ``spec.mcp_servers.<name>.inherit_env`` mode (see
:mod:`..config._mcp_inherit_env`): an entry that inherits nothing receives no
launch-env refill and no SAC-generated ``PGPASSFILE``, and the credential
check applies only to entries whose FINAL env carries a PostgreSQL DSN.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..config import AgentConfig
from ..config._mcp_inherit_env import inherits, server_modes

_MCP_NON_SECRET_ENV = {
    "PGPASSFILE",
    "PGUSER",
    "SCITEX_CARDS_AGENT_ID",
    "SCITEX_CARDS_SCOPE",
    "SCITEX_CARDS_NOTIFY_DSN",
    "SCITEX_STORE_DSN",
}


_MCP_SAC_ENV_REFS = {
    "SAC_LISTEN_BASE_URL": "${env:SAC_LISTEN_BASE_URL}",
    "SAC_LISTEN_BEARER": "${env:SAC_LISTEN_BEARER}",
    "SAC_NAME": "${env:SAC_NAME}",
}


def _host_pgpass_source(
    container_path: str,
    *,
    launch_argv: Sequence[str],
) -> Path | None:
    """Resolve the host source visible at a container path after all binds."""
    rendered = Path(container_path).expanduser()
    argv = [str(value) for value in launch_argv]
    # Apptainer keeps the first bind for an exact destination.  Preserve that
    # precedence before choosing the most-specific mount covering the file.
    sources_by_destination: dict[Path, Path] = {}
    for index, arg in enumerate(argv):
        declaration = ""
        if arg in {"--bind", "-B"} and index + 1 < len(argv):
            declaration = argv[index + 1]
        elif arg.startswith("--bind="):
            declaration = arg.split("=", 1)[1]
        if not declaration:
            continue
        for binding in declaration.split(","):
            parts = binding.split(":", 2)
            if len(parts) < 2:
                continue
            source, destination = map(Path, parts[:2])
            sources_by_destination.setdefault(destination, source.expanduser())

    covering: list[tuple[Path, Path, Path]] = []
    for destination, source in sources_by_destination.items():
        try:
            relative = rendered.relative_to(destination)
        except ValueError:
            continue
        covering.append((destination, source, relative))
    if not covering:
        return None
    _, source, relative = max(
        covering,
        key=lambda item: len(item[0].parts),
    )
    return source / relative


def _validate_mcp_pg_credentials(
    servers: dict[str, dict[str, Any]],
    *,
    launch_argv: Sequence[str],
) -> None:
    """Verify generated roleless-DSN MCP entries have a usable login."""
    from ._pg_identity_env import PgIdentityCredentialError, pgpass_has_role

    checked: set[tuple[str, str]] = set()
    for server in servers.values():
        declared = server.get("env")
        if not isinstance(declared, dict):
            continue
        postgres_dsns = tuple(
            value
            for key in ("SCITEX_CARDS_NOTIFY_DSN", "SCITEX_STORE_DSN")
            if (value := str(declared.get(key, ""))).startswith(
                ("postgresql://", "postgres://")
            )
        )
        if not postgres_dsns:
            continue
        role = str(declared.get("PGUSER", "")).strip()
        passfile = str(declared.get("PGPASSFILE", "")).strip()
        if not role or not passfile:
            raise PgIdentityCredentialError(
                "Hermes MCP received a roleless PostgreSQL DSN without both "
                "PGUSER and PGPASSFILE"
            )
        identity = (passfile, role)
        if identity not in checked:
            source = _host_pgpass_source(
                passfile,
                launch_argv=launch_argv,
            )
            if source is None or not pgpass_has_role(source, role, dsns=postgres_dsns):
                raise PgIdentityCredentialError(
                    f"PostgreSQL identity {role!r} has no credential in the "
                    "finalized launch bind sources; "
                    "provision the project role before starting the agent"
                )
            checked.add(identity)


def _bind_mcp_runtime_env(
    config: AgentConfig, servers: dict[str, dict[str, Any]]
) -> None:
    from ._hermes_sac_runtime import bind_sac_mcp_command

    bind_sac_mcp_command(config, servers)
    from ._board_identity_env import raw_args_env
    from ._fleet_env import effective_env

    sealed = getattr(config, "sandbox", None) is not None
    # A sealed run (spec.sandbox) gets no fleet-default env, in its MCP
    # entries either: config.yaml is readable inside the container.
    runtime_env = effective_env(config, defaults={}) if sealed else effective_env(config)
    if sealed:
        from .._sandbox import allowed_env_names

        allowed = allowed_env_names(config)
        runtime_env = {k: v for k, v in runtime_env.items() if k in allowed}
    apptainer = getattr(config, "apptainer", None)
    # raw_args are appended after curated --env flags in the production argv,
    # so they are the real last-wins launch environment.  apply_pg_identity()
    # deliberately suppresses a generated PGUSER when one is declared here;
    # fold the same declarations back in before writing the Hermes MCP env.
    runtime_env.update(raw_args_env(getattr(apptainer, "raw_args", None)))
    from ._pg_identity_credentials import (
        DEFAULT_CONTAINER_PGPASSFILE,
        PG_PASSFILE_ENV,
    )

    # Per-entry inherit_env, from the SPEC (the key is stripped from the
    # .mcp.json the servers were translated from). Entries SAC did not get
    # from spec.mcp_servers inherit everything, as before.
    modes = server_modes(getattr(config, "mcp_servers", None))
    for name, server in servers.items():
        mode = modes.get(name, True)
        declared = server.get("env")
        if not isinstance(declared, dict):
            declared = {}
            server["env"] = declared
        command = Path(str(server.get("command", ""))).name
        is_cards = name in {"cards", "scitex-cards"} or command == "scitex-cards"
        is_sac = name in {"sac", "scitex-agent-container"} or command == "sac"
        is_cct = name == "claude-code-telegrammer"
        if is_cards or is_sac:
            # This was the deprecated Cards-specific store alias.  Retaining
            # it permits a generated profile to disagree with the one shared
            # SciTeX store, so do not carry it across the Hermes boundary.
            declared.pop("SCITEX_CARDS_DB", None)
        if is_sac:
            declared.update(_MCP_SAC_ENV_REFS)
        keys = (
            _MCP_NON_SECRET_ENV
            if is_cards or is_sac
            else {"PGPASSFILE", "PGUSER", "SCITEX_STORE_DSN"}
            if is_cct
            else _MCP_NON_SECRET_ENV.intersection(declared)
        )
        for key in (k for k in keys if inherits(mode, k)):
            value = runtime_env.get(key)
            if value is not None:
                declared[key] = str(value)
        has_postgres = any(
            str(declared.get(key, "")).startswith(("postgresql://", "postgres://"))
            for key in ("SCITEX_CARDS_NOTIFY_DSN", "SCITEX_STORE_DSN")
        )
        current_passfile = str(declared.get(PG_PASSFILE_ENV, "")).strip()
        # inherit_env: false = no SAC-generated credential either; a DSN the
        # entry declares itself must bring its own PGUSER + PGPASSFILE.
        if (
            mode is not False
            and has_postgres
            and current_passfile in {"", "${PGPASSFILE}"}
        ):
            declared[PG_PASSFILE_ENV] = DEFAULT_CONTAINER_PGPASSFILE


__all__ = [
    "_MCP_NON_SECRET_ENV",
    "_MCP_SAC_ENV_REFS",
    "_bind_mcp_runtime_env",
    "_host_pgpass_source",
    "_validate_mcp_pg_credentials",
]
