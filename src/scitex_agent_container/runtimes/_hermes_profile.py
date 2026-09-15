"""Materialize one isolated Hermes profile from a loaded SAC agent config."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import yaml

from ..config import AgentConfig
from ..config._hermes_config import compile_hermes_config
from ..config._launch_plan import (
    DelegationPolicy,
    Endpoint,
    LaunchPlan,
    ResolvedEngine,
)
from ._apptainer_provider import resolve_provider_api_key
from ._to_home import deploy_to_home
from ._to_home_overlay import deploy_to_home_overlay, resolve_overlay_upper_home

API_KEY_FILE = "hermes-api.key"
API_PORT_FILE = "hermes-api.port"
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


def validate_hermes_tui_profile(
    config: AgentConfig, *, state_dir: Path, launch_argv: Sequence[str]
) -> None:
    """Validate the materialized profile against the real finalized argv."""
    targets = [state_dir / "home"]
    upper = resolve_overlay_upper_home(config)
    if upper is not None:
        targets.append(upper)
    documents: list[dict[str, Any]] = []
    for target in targets:
        path = target / ".hermes" / "config.yaml"
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            documents.append(document)
    if not documents:
        raise RuntimeError(
            f"Hermes profile was not materialized for {config.name!r} before launch"
        )
    for document in documents:
        servers = document.get("mcp_servers")
        if isinstance(servers, dict):
            _validate_mcp_pg_credentials(servers, launch_argv=launch_argv)


def _launch_plan(config: AgentConfig, *, launch_mode: str = "headless") -> LaunchPlan:
    provider = config.claude.provider
    base_url = str(provider.base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Hermes requires the selected engine provider.base_url")
    if urlsplit(base_url).path.rstrip("/").endswith("/responses"):
        protocol = "openai-responses"
        endpoint_url = base_url
    else:
        protocol = "openai-chat-completions"
        api_root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
        endpoint_url = f"{api_root}/chat/completions"
    endpoint = Endpoint(
        protocol=protocol,
        url=endpoint_url,
        auth_kind="bearer",
        auth_env=str(provider.auth_token_env or ""),
    )
    engine = ResolvedEngine(
        key=str(config.engine_key or config.model),
        model_id=str(config.model),
        endpoints=(endpoint,),
        context_window_tokens=config.max_context_tokens,
        reasoning_effort=str(config.reasoning_effort or "") or None,
        upstream_deadline_seconds=config.upstream_deadline_seconds,
        client_abandonment_seconds=config.client_abandonment_seconds,
    )
    return LaunchPlan(
        "hermes",
        launch_mode,
        "apptainer",
        engine,
        endpoint,
        may_spawn=config.lineage.may_spawn,
        delegation=DelegationPolicy(
            max_concurrent_children=config.delegation.max_concurrent_children,
            worktree_isolation=config.delegation.worktree_isolation,
        ),
        agent_name=config.name,
    )


def _selected_server_names(channels: Sequence[str] | None) -> set[str]:
    """Return the MCP names explicitly selected by ``server:<name>`` rails."""
    selected: set[str] = set()
    for raw in channels or ():
        channel = str(raw).strip()
        if channel.startswith("server:") and channel.removeprefix("server:"):
            selected.add(channel.removeprefix("server:"))
    return selected


def _mcp_servers(
    home: Path, *, channels: Sequence[str] | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    source = home / ".mcp.json"
    if not source.is_file():
        return {}, []
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"cannot translate Hermes MCP config {source}: {exc}"
        ) from exc
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}, []
    translated: dict[str, dict[str, Any]] = {}
    eager_toolsets: list[str] = []
    selected = _selected_server_names(channels)
    for name, entry in servers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        if entry.get("alwaysLoad") is not True and name not in selected:
            continue
        eager_toolsets.append(f"mcp-{name}")
        rendered = {
            key: value
            for key, value in entry.items()
            if key not in {"type", "alwaysLoad"}
        }
        command = Path(str(rendered.get("command", ""))).name
        is_cards = name in {"cards", "scitex-cards"} or command == "scitex-cards"
        args = rendered.get("args")
        if (
            is_cards
            and isinstance(args, list)
            and args[:2] == ["mcp", "start"]
            and "--tools-only" not in args
        ):
            # Hermes receives Cards notifications through SAC's durable,
            # terminal-visible ingress worker. Running Cards' Claude-specific
            # poller here would consume the same inbox a second time and emit
            # custom notifications Hermes ignores. Keep the stdio tools and
            # their SCITEX_CARDS_AGENT_ID, disable only that duplicate poller.
            rendered["args"] = [*args, "--tools-only"]
        translated[name] = rendered
    return translated, eager_toolsets


def _bind_mcp_runtime_env(
    config: AgentConfig, servers: dict[str, dict[str, Any]]
) -> None:
    from ._board_identity_env import raw_args_env
    from ._fleet_env import effective_env

    runtime_env = effective_env(config)
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

    for name, server in servers.items():
        declared = server.get("env")
        if not isinstance(declared, dict):
            declared = {}
            server["env"] = declared
        command = Path(str(server.get("command", ""))).name
        is_cards = name in {"cards", "scitex-cards"} or command == "scitex-cards"
        is_sac = name in {"sac", "scitex-agent-container"} or command == "sac"
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
            else _MCP_NON_SECRET_ENV.intersection(declared)
        )
        for key in keys:
            value = runtime_env.get(key)
            if value is not None:
                declared[key] = str(value)
        has_postgres = any(
            str(declared.get(key, "")).startswith(("postgresql://", "postgres://"))
            for key in ("SCITEX_CARDS_NOTIFY_DSN", "SCITEX_STORE_DSN")
        )
        current_passfile = str(declared.get(PG_PASSFILE_ENV, "")).strip()
        if has_postgres and current_passfile in {"", "${PGPASSFILE}"}:
            declared[PG_PASSFILE_ENV] = DEFAULT_CONTAINER_PGPASSFILE


def _sac_profile_env(
    config: AgentConfig, servers: dict[str, dict[str, Any]]
) -> dict[str, str]:
    has_sac = any(
        name in {"sac", "scitex-agent-container"}
        or Path(str(server.get("command", ""))).name == "sac"
        for name, server in servers.items()
    )
    if not has_sac:
        return {}

    from .._listen._config import listen_base_url
    from ._apptainer_build import _listen_token_path, _read_listen_bearer

    bearer = _read_listen_bearer()
    if not bearer:
        raise RuntimeError(
            "Hermes SAC MCP requires the host listen bearer, but token file "
            f"{_listen_token_path()} is absent or empty"
        )
    return {
        "SAC_LISTEN_BASE_URL": listen_base_url(),
        "SAC_LISTEN_BEARER": bearer,
        "SAC_NAME": config.name,
    }


def _write_profile_env(path: Path, values: dict[str, str]) -> None:
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Hermes profile env value for {key} contains a newline")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        body = "".join(f"{key}={value}\n" for key, value in values.items())
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def ensure_api_key(state_dir: Path) -> str:
    path = state_dir / API_KEY_FILE
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 16:
            return value
    value = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{value}\n".encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return value


def materialize_hermes_profile(
    config: AgentConfig, *, state_dir: Path, api_port: int
) -> tuple[str, list[Path]]:
    """Write matching Hermes profiles to every possible container-home backing."""
    state_dir.mkdir(parents=True, exist_ok=True)
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    deploy_to_home(config, str(home))
    overlay_home = deploy_to_home_overlay(config)
    api_key = ensure_api_key(state_dir)
    provider_key = resolve_provider_api_key(config)
    plan = _launch_plan(config)
    max_turns = max(1, int(getattr(config.autonomous, "max_turns", 50) or 50))
    rendered = compile_hermes_config(
        plan,
        workdir=str(config.workdir),
        max_turns=max_turns,
        run_budget_seconds=config.hermes_run_budget_seconds,
        approval_mode="off",
        compression=config.hermes_compression,
        background_review=config.hermes_background_review,
    )
    rendered["gateway"] = {
        "api_server": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": api_port,
            "max_concurrent_runs": 1,
            "model_name": config.name,
        }
    }
    servers, eager_toolsets = _mcp_servers(
        home, channels=getattr(config.claude, "channels", None)
    )
    if servers:
        _bind_mcp_runtime_env(config, servers)
    from ._hermes_cct import wire_hermes_cct_rail

    cct_env = wire_hermes_cct_rail(config, home=home, servers=servers)
    if servers:
        rendered["mcp_servers"] = servers
    if eager_toolsets:
        rendered["toolsets"] = [*rendered.get("toolsets", []), *eager_toolsets]
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        targets.append(resolved_upper)
    from ._pg_identity_credentials import materialize_project_pgpass

    materialize_project_pgpass(config, home_backings=targets, servers=servers)
    env_name = plan.endpoint.auth_env
    profile_env = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(api_port),
        "API_SERVER_KEY": api_key,
        env_name: provider_key,
        **_sac_profile_env(config, servers),
        **cct_env,
    }
    for target in targets:
        profile = target / ".hermes"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text(
            yaml.safe_dump(rendered, sort_keys=False), encoding="utf-8"
        )
        _write_profile_env(profile / ".env", profile_env)
    (state_dir / API_PORT_FILE).write_text(f"{api_port}\n", encoding="utf-8")
    return api_key, targets


def materialize_hermes_tui_profile(
    config: AgentConfig, *, state_dir: Path, deploy_home: bool = True
) -> list[Path]:
    """Write the isolated profile consumed by an official Hermes TUI."""
    state_dir.mkdir(parents=True, exist_ok=True)
    ensure_api_key(state_dir)
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    if deploy_home:
        deploy_to_home(config, str(home))
        overlay_home = deploy_to_home_overlay(config)
    else:
        overlay_home = resolve_overlay_upper_home(config)
    provider_key = resolve_provider_api_key(config)
    plan = _launch_plan(config, launch_mode="tui")
    max_turns = max(1, int(getattr(config.autonomous, "max_turns", 50) or 50))
    rendered = compile_hermes_config(
        plan,
        workdir=str(config.workdir),
        max_turns=max_turns,
        run_budget_seconds=config.hermes_run_budget_seconds,
        approval_mode="off",
        compression=config.hermes_compression,
        background_review=config.hermes_background_review,
    )
    servers, eager_toolsets = _mcp_servers(
        home, channels=getattr(config.claude, "channels", None)
    )
    if servers:
        _bind_mcp_runtime_env(config, servers)
    from ._hermes_cct import wire_hermes_cct_rail

    cct_env = wire_hermes_cct_rail(config, home=home, servers=servers)
    if servers:
        rendered["mcp_servers"] = servers
    if eager_toolsets:
        rendered["toolsets"] = [*rendered.get("toolsets", []), *eager_toolsets]
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        targets.append(resolved_upper)
    from ._pg_identity_credentials import materialize_project_pgpass

    materialize_project_pgpass(config, home_backings=targets, servers=servers)
    profile_env = {
        plan.endpoint.auth_env: provider_key,
        **_sac_profile_env(config, servers),
        **cct_env,
    }
    for target in targets:
        profile = target / ".hermes"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text(
            yaml.safe_dump(rendered, sort_keys=False), encoding="utf-8"
        )
        _write_profile_env(profile / ".env", profile_env)
    return targets


__all__ = [
    "API_KEY_FILE",
    "API_PORT_FILE",
    "ensure_api_key",
    "materialize_hermes_profile",
    "materialize_hermes_tui_profile",
    "validate_hermes_tui_profile",
]
