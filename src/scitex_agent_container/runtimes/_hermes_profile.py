"""Materialize one isolated Hermes profile from a loaded SAC agent config."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from ..config import AgentConfig
from ..config._hermes_config import compile_hermes_config
from ..config._launch_plan import Endpoint, LaunchPlan, ResolvedEngine
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
    "SCITEX_CARDS_DB",
    "SCITEX_STORE_DSN",
}
_MCP_SAC_ENV_REFS = {
    "SAC_LISTEN_BASE_URL": "${env:SAC_LISTEN_BASE_URL}",
    "SAC_LISTEN_BEARER": "${env:SAC_LISTEN_BEARER}",
    "SAC_NAME": "${env:SAC_NAME}",
}


def _launch_plan(config: AgentConfig, *, launch_mode: str = "headless") -> LaunchPlan:
    provider = config.claude.provider
    base_url = str(provider.base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Hermes requires the selected engine provider.base_url")
    api_root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    endpoint = Endpoint(
        protocol="openai-chat-completions",
        url=f"{api_root}/chat/completions",
        auth_kind="bearer",
        auth_env=str(provider.auth_token_env or ""),
    )
    engine = ResolvedEngine(
        key=str(config.engine_key or config.model),
        model_id=str(config.model),
        endpoints=(endpoint,),
        context_window_tokens=config.max_context_tokens,
        reasoning_effort=str(config.reasoning_effort or "") or None,
    )
    return LaunchPlan("hermes", launch_mode, "apptainer", engine, endpoint)


def _mcp_servers(home: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    source = home / ".mcp.json"
    if not source.is_file():
        return {}, []
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot translate Hermes MCP config {source}: {exc}") from exc
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}, []
    translated: dict[str, dict[str, Any]] = {}
    eager_toolsets: list[str] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        if entry.get("alwaysLoad") is not True:
            continue
        eager_toolsets.append(f"mcp-{name}")
        translated[name] = {
            key: value
            for key, value in entry.items()
            if key not in {"type", "alwaysLoad"}
        }
    return translated, eager_toolsets


def _bind_mcp_runtime_env(
    config: AgentConfig, servers: dict[str, dict[str, Any]]
) -> None:
    from ._fleet_env import effective_env

    runtime_env = effective_env(config)
    for name, server in servers.items():
        declared = server.get("env")
        if not isinstance(declared, dict):
            declared = {}
            server["env"] = declared
        command = Path(str(server.get("command", ""))).name
        is_cards = name in {"cards", "scitex-cards"} or command == "scitex-cards"
        is_sac = name in {"sac", "scitex-agent-container"} or command == "sac"
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
        run_budget_seconds=1200,
        approval_mode="off",
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
    servers, eager_toolsets = _mcp_servers(home)
    if servers:
        _bind_mcp_runtime_env(config, servers)
        rendered["mcp_servers"] = servers
    if eager_toolsets:
        rendered["toolsets"] = [*rendered.get("toolsets", []), *eager_toolsets]
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        targets.append(resolved_upper)
    env_name = plan.endpoint.auth_env
    profile_env = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(api_port),
        "API_SERVER_KEY": api_key,
        env_name: provider_key,
        **_sac_profile_env(config, servers),
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
    config: AgentConfig, *, state_dir: Path
) -> list[Path]:
    """Write the isolated profile consumed by an official Hermes TUI."""
    state_dir.mkdir(parents=True, exist_ok=True)
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    deploy_to_home(config, str(home))
    overlay_home = deploy_to_home_overlay(config)
    provider_key = resolve_provider_api_key(config)
    plan = _launch_plan(config, launch_mode="tui")
    max_turns = max(1, int(getattr(config.autonomous, "max_turns", 50) or 50))
    rendered = compile_hermes_config(
        plan,
        workdir=str(config.workdir),
        max_turns=max_turns,
        run_budget_seconds=1200,
        approval_mode="off",
    )
    servers, eager_toolsets = _mcp_servers(home)
    if servers:
        _bind_mcp_runtime_env(config, servers)
        rendered["mcp_servers"] = servers
    if eager_toolsets:
        rendered["toolsets"] = [*rendered.get("toolsets", []), *eager_toolsets]
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        targets.append(resolved_upper)
    profile_env = {
        plan.endpoint.auth_env: provider_key,
        **_sac_profile_env(config, servers),
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
]
