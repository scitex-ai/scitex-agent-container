"""Config loaders for v1 and v2 YAML formats."""

from __future__ import annotations

from pathlib import Path

from ._host import resolve_hostname, substitute_hostnames
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
    parse_remote,
    parse_restart,
    parse_scheduling,
    parse_skills,
    parse_slurm,
    parse_startup,
    parse_startup_commands,
    parse_telegram,
    parse_watchdog,
)
from ._types import AgentConfig, SchedulingSpec

# Host-aware fallback chain for `venv: auto` resolution.
# Tried in order; first existing path wins. Empty string means no venv
# activation (raw shell). The chain is intentionally short and biased
# toward the conventions actually in use across the fleet (NAS/WSL =
# ~/.venv-3.11, MBA = ~/.venv). Adding a new host with a different
# convention requires extending this list.
#
# Filed via scitex-agent-container#40 (head-mba 2026-04-16) after the
# fleet-lead.yaml `venv: auto` shell-source-fail incident on NAS
# (head-nas msg#12877; head-mba msg#12879 root cause).
_VENV_AUTO_FALLBACK_CHAIN = ("~/.venv-3.11", "~/.venv")

# Default workdir layout: sac's own state root. Per-agent runtime state
# (CLAUDE.md, .mcp.json, .claude/) lives at
# ``~/.scitex/agent-container/workspaces/<effective-id>/``. External
# orchestrators that want a different layout can override via ``spec.workdir``.
_DEFAULT_WORKDIR_RUNTIME = "~/.scitex/agent-container/workspaces/{name}"


def _resolve_venv(venv: str) -> str:
    """Resolve `venv: auto` to the first existing virtualenv on this host.

    Returns the original value unchanged unless it equals "auto" (case
    insensitive). For "auto", probes ~/.venv-3.11 then ~/.venv and
    returns the first one whose `bin/activate` exists. If none exist,
    returns empty string (runtime treats as "no venv activation"), which
    is still safer than letting the shell try to source a missing path.
    """
    if not isinstance(venv, str) or venv.strip().lower() != "auto":
        return venv
    for candidate in _VENV_AUTO_FALLBACK_CHAIN:
        if (Path(candidate).expanduser() / "bin" / "activate").exists():
            return candidate
    return ""


def compose_effective_name(
    raw_name: str, scheduling: SchedulingSpec | None, hostname: str
) -> str:
    """Return the effective agent id given metadata.name + scheduling + host.

    Rules:
      * ``singleton`` mode: the bare ``raw_name`` (host-pin is enforced at
        launch time, not encoded in the id).
      * ``per-host`` mode (default): append ``-<hostname>`` unless the name
        already ends with ``-<hostname>`` (idempotent — protects legacy
        flat-layout names like ``head-ywata-note-win`` which are already
        host-suffixed).
    """
    if scheduling is not None and scheduling.mode == "singleton":
        return raw_name
    suffix = f"-{hostname}"
    if raw_name.endswith(suffix) or raw_name == hostname:
        return raw_name
    return f"{raw_name}{suffix}"


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
        venv=_resolve_venv(spec.get("venv", "")),
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
        remote=parse_remote(spec),
        skills=parse_skills(spec),
        startup_commands=parse_startup_commands(spec),
        startup=parse_startup(spec),
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        multiplexer=spec.get("multiplexer", "screen"),
        slurm=parse_slurm(spec),
        config_path=str(path),
    )


def load_v2(raw: dict, path: Path) -> AgentConfig:
    """Load a scitex-agent-container/v2 config with auto-derived defaults.

    Substitutes ``${HOSTNAME}`` in every string field before dataclass
    construction, and composes the effective agent id from ``metadata.name``
    + ``spec.scheduling`` so one canonical YAML per role can be shared
    across hosts.
    """
    # Only walk-and-substitute hostname placeholders when the YAML opts in
    # via an explicit ``spec.scheduling`` block. Legacy v2 YAMLs without
    # scheduling keep the pre-change code path (no substitution, no
    # effective-id composition, no host resolution required).
    scheduling, explicit_scheduling = parse_scheduling(raw.get("spec", {}) or {})

    if explicit_scheduling:
        hostname = resolve_hostname()
        raw = substitute_hostnames(raw, hostname)
    else:
        hostname = ""

    metadata = raw.get("metadata", {})
    spec = raw.get("spec", {})
    raw_name = metadata["name"]
    labels = metadata.get("labels", {}) or {}

    # Compose the effective id used everywhere downstream (systemd, screen/
    # tmux, workdir, registry keys). Only when scheduling is explicit —
    # otherwise keep the raw metadata.name as-is for backward compatibility.
    if explicit_scheduling:
        name = compose_effective_name(raw_name, scheduling, hostname)
    else:
        name = raw_name

    # Auto-derive workdir (user can override).
    # Default lives under runtime/workspaces/ (2026-04-17 layout).
    workdir = spec.get("workdir")
    if workdir is None:
        workdir = _DEFAULT_WORKDIR_RUNTIME.format(name=name)

    # Auto-derive screen_name: {name} (not cld-{name})
    screen_raw = spec.get("screen", {}) or {}
    screen_name = screen_raw.get("name", name)

    # Auto-derive env: user values override auto-derived.
    # Only sac's own namespace is injected. External consumers (orochi etc.)
    # declare their own env vars explicitly in agent YAML's ``spec.env`` if
    # they want them set.
    auto_env: dict[str, str] = {
        "CLAUDE_AGENT_ID": name,
        "SCITEX_AGENT_CONTAINER_AGENT": name,
    }
    if labels.get("role"):
        auto_env["CLAUDE_AGENT_ROLE"] = labels["role"]
    model = str(spec.get("model", "sonnet") or "sonnet")
    display_model = MODEL_DISPLAY_NAMES.get(model, model)
    auto_env["SCITEX_AGENT_CONTAINER_MODEL"] = display_model

    user_env = spec.get("env", {}) or {}
    merged_env = {**auto_env, **user_env}

    # Auto-derive hooks: prepend mkdir for workdir
    hooks = parse_hooks(spec)
    expanded = str(Path(workdir).expanduser())
    mkdir_cmd = f"mkdir -p {expanded}/.claude"
    if mkdir_cmd not in hooks.get("pre_start", []):
        hooks.setdefault("pre_start", []).insert(0, mkdir_cmd)

    # Parse mcp_servers with metadata interpolation (uses effective name)
    mcp_metadata = {**metadata, "name": name}
    mcp_servers = interpolate_mcp_servers(spec.get("mcp_servers", {}), mcp_metadata)

    return AgentConfig(
        name=name,
        runtime=spec.get("runtime", "claude-code"),
        model=model,
        workdir=workdir,
        venv=_resolve_venv(spec.get("venv", "")),
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
        remote=parse_remote(spec),
        slurm=parse_slurm(spec),
        skills=parse_skills(spec),
        startup_commands=parse_startup_commands(spec),
        startup=parse_startup(spec),
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        mcp_servers=mcp_servers,
        multiplexer=spec.get("multiplexer", "screen"),
        scheduling=scheduling,
        config_path=str(path),
    )
