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
    parse_hosts_spec,
    parse_listen,
    parse_remote,
    parse_restart,
    parse_skills,
    parse_slurm,
    parse_startup,
    parse_startup_commands,
    parse_telegram,
    parse_watchdog,
)
from ._types import AgentConfig, HostsSpec

# Default workdir layout: sac's own state root. Per-agent runtime state
# (CLAUDE.md, .mcp.json, .claude/) lives at
# ``~/.scitex/agent-container/workspaces/<effective-id>/``. External
# orchestrators that want a different layout can override via ``spec.workdir``.
_DEFAULT_WORKDIR_RUNTIME = "~/.scitex/agent-container/workspaces/{name}"


def _name_from_path(path: Path | str) -> str:
    """Derive the agent name from the YAML path.

    Convention: each agent lives in its own directory ``<name>/<name>.yaml``.
    The directory name IS the agent identifier — single source of truth.
    YAMLs do not carry a redundant ``metadata.name`` field.
    """
    return Path(path).parent.name


def _resolve_python_venv(venv: str | list[str] | None) -> str:
    """Resolve ``spec.python-venv`` to a single venv path on this host.

    Accepts:
      * empty/None: no venv activation (returns "").
      * single string: literal path; must exist or RuntimeError.
      * list of strings: explicit fallback chain — first existing path
        wins. If none exist, raises RuntimeError (no silent fallback).

    The fallback chain is intentionally per-agent (in the YAML), not a
    sac-internal default — different agents may want different chains,
    and putting it in the YAML keeps the precedence visible to readers.
    """
    if venv is None or venv == "" or venv == []:
        return ""

    if isinstance(venv, str):
        if (Path(venv).expanduser() / "bin" / "activate").exists():
            return venv
        raise RuntimeError(
            f"python-venv {venv!r} has no bin/activate on this host. "
            "Set an existing path or use a list for a fallback chain."
        )

    if isinstance(venv, list):
        if not all(isinstance(p, str) for p in venv):
            raise RuntimeError(f"python-venv list must contain strings, got: {venv!r}")
        for candidate in venv:
            if (Path(candidate).expanduser() / "bin" / "activate").exists():
                return candidate
        raise RuntimeError(
            f"python-venv chain {venv!r} matched no existing venv on this "
            "host. Create one of these paths or extend the chain."
        )

    raise RuntimeError(
        f"python-venv must be a string or list of strings, got "
        f"{type(venv).__name__}: {venv!r}"
    )


def compose_effective_name(
    raw_name: str, hosts_spec: HostsSpec | None, hostname: str
) -> str:
    """Return the effective agent id given dir-derived name + host/hosts + host.

    Rules:
      * If ``hosts:`` is set (multi-instance), append ``-<hostname>`` so
        each host's instance has a unique id. Idempotent — names that
        already end with ``-<hostname>`` are not double-suffixed.
      * Otherwise (``host:`` set, or both empty = local singleton): keep
        the bare ``raw_name``. Singleton id stays stable across hosts.
    """
    is_multi = (
        hosts_spec is not None and hosts_spec.hosts != "" and hosts_spec.hosts != []
    )
    if not is_multi:
        return raw_name
    suffix = f"-{hostname}"
    if raw_name.endswith(suffix) or raw_name == hostname:
        return raw_name
    return f"{raw_name}{suffix}"


def load_v3(raw: dict, path: Path) -> AgentConfig:
    """Load a scitex-agent-container/v3 config with auto-derived defaults.

    v3 changes from v2:
      * ``metadata.name`` rejected (dir-as-SSoT — name from parent dir)
      * ``spec.scheduling`` block dropped; ``spec.host`` / ``spec.hosts``
        used directly
      * ``spec.python-venv`` (was ``spec.venv``); takes string or list
      * ``spec.health.method: multiplexer-alive`` (was ``screen-alive``)

    No backward compatibility — old apiVersions raise loud validation
    errors at config-load time.
    """
    spec = raw.get("spec", {}) or {}
    hosts_spec = parse_hosts_spec(spec)

    # ${HOSTNAME} substitution only meaningful when this is a multi-host
    # template (``hosts:`` set). Singletons run on the canonical host name.
    is_multi = hosts_spec.hosts != "" and hosts_spec.hosts != []
    hostname = resolve_hostname() if is_multi else ""
    if is_multi:
        raw = substitute_hostnames(raw, hostname)
        spec = raw.get("spec", {}) or {}

    metadata = raw.get("metadata", {}) or {}
    raw_name = _name_from_path(path)
    labels = metadata.get("labels", {}) or {}

    name = compose_effective_name(raw_name, hosts_spec, hostname)

    # Auto-derive workdir (user can override).
    # Default lives under runtime/workspaces/ (2026-04-17 layout).
    workdir = spec.get("workdir")
    if workdir is None:
        workdir = _DEFAULT_WORKDIR_RUNTIME.format(name=name)

    # Auto-derive screen_name: {name} (not cld-{name})
    screen_raw = spec.get("screen", {}) or {}
    screen_name = screen_raw.get("name", name)

    # Auto-derive env: user values override auto-derived.
    # sac owns identity injection — ``SCITEX_OROCHI_AGENT`` is stamped here
    # so the orochi startup-protocol identity check is authoritative against
    # the agent YAML, not whatever value leaked from the parent shell (see
    # scitex-orochi fleet DM 2026-04-22, ywatanabe-approved). Other orochi
    # config (tokens, model labels, channels) remains the caller's concern
    # and is declared explicitly in agent YAML's ``spec.env``.
    auto_env: dict[str, str] = {
        "CLAUDE_AGENT_ID": name,
        "SCITEX_AGENT_CONTAINER_AGENT": name,
        "SCITEX_OROCHI_AGENT": name,
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
        python_venv=_resolve_python_venv(spec.get("python-venv", "")),
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
        multiplexer=spec.get("multiplexer", "tmux"),
        hosts_spec=hosts_spec,
        config_path=str(path),
    )
