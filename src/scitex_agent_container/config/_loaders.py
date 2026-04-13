"""Config loaders for v1 and v2 YAML formats."""

from __future__ import annotations

from pathlib import Path

from ._parsers import (
    MODEL_DISPLAY_NAMES,
    interpolate_mcp_servers,
    parse_claude,
    parse_container,
    parse_context_management,
    parse_extensions,
    parse_health,
    parse_hooks,
    parse_listen,
    parse_orochi,
    parse_remote,
    parse_restart,
    parse_skills,
    parse_startup_commands,
    parse_telegram,
    parse_watchdog,
)
from ._types import AgentConfig


def load_v1(raw: dict, path: Path) -> AgentConfig:
    """Load a cld-agent/v1 config."""
    metadata = raw.get("metadata", {})
    spec = raw.get("spec", {})

    screen_raw = spec.get("screen", {}) or {}
    screen_name = screen_raw.get("name", "")

    return AgentConfig(
        name=metadata["name"],
        runtime=spec.get("runtime", "claude-code"),
        model=spec.get("model", "sonnet"),
        workdir=spec.get("workdir", "~/proj"),
        venv=spec.get("venv", ""),
        env=spec.get("env", {}) or {},
        screen_name=screen_name,
        labels=metadata.get("labels", {}) or {},
        container=parse_container(spec),
        claude=parse_claude(spec),
        health=parse_health(spec),
        watchdog=parse_watchdog(spec),
        restart=parse_restart(spec),
        hooks=parse_hooks(spec),
        telegram=parse_telegram(spec),
        orochi=parse_orochi(spec),
        remote=parse_remote(spec),
        skills=parse_skills(spec),
        startup_commands=parse_startup_commands(spec),
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        multiplexer=spec.get("multiplexer", "screen"),
        config_path=str(path),
    )


def load_v2(raw: dict, path: Path) -> AgentConfig:
    """Load a scitex-agent-container/v2 config with auto-derived defaults."""
    metadata = raw.get("metadata", {})
    spec = raw.get("spec", {})
    name = metadata["name"]
    labels = metadata.get("labels", {}) or {}

    # Auto-derive workdir (user can override)
    workdir = spec.get("workdir", f"~/.scitex/orochi/workspaces/{name}")

    # Auto-derive screen_name: {name} (not cld-{name})
    screen_raw = spec.get("screen", {}) or {}
    screen_name = screen_raw.get("name", name)

    # Auto-derive env: user values override auto-derived
    auto_env: dict[str, str] = {
        "CLAUDE_AGENT_ID": name,
        "SCITEX_OROCHI_AGENT": name,
    }
    if labels.get("role"):
        auto_env["CLAUDE_AGENT_ROLE"] = labels["role"]
    model = str(spec.get("model", "sonnet") or "sonnet")
    display_model = MODEL_DISPLAY_NAMES.get(model, model)
    auto_env["SCITEX_OROCHI_MODEL"] = display_model

    user_env = spec.get("env", {}) or {}
    merged_env = {**auto_env, **user_env}

    # Auto-derive hooks: prepend mkdir for workdir
    hooks = parse_hooks(spec)
    expanded = str(Path(workdir).expanduser())
    mkdir_cmd = f"mkdir -p {expanded}/.claude"
    if mkdir_cmd not in hooks.get("pre_start", []):
        hooks.setdefault("pre_start", []).insert(0, mkdir_cmd)

    # Parse mcp_servers with metadata interpolation
    mcp_servers = interpolate_mcp_servers(spec.get("mcp_servers", {}), metadata)

    return AgentConfig(
        name=name,
        runtime=spec.get("runtime", "claude-code"),
        model=model,
        workdir=workdir,
        venv=spec.get("venv", ""),
        env=merged_env,
        screen_name=screen_name,
        labels=labels,
        container=parse_container(spec),
        claude=parse_claude(spec),
        health=parse_health(spec),
        watchdog=parse_watchdog(spec),
        restart=parse_restart(spec),
        hooks=hooks,
        telegram=parse_telegram(spec),
        orochi=parse_orochi(spec),
        remote=parse_remote(spec),
        skills=parse_skills(spec),
        startup_commands=parse_startup_commands(spec),
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        mcp_servers=mcp_servers,
        multiplexer=spec.get("multiplexer", "screen"),
        config_path=str(path),
    )
