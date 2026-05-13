"""Config loader for scitex-agent-container/v3 YAML."""

from __future__ import annotations

from pathlib import Path

from ._host import resolve_hostname, substitute_hostnames
from ._parsers import (
    MODEL_DISPLAY_NAMES,
    interpolate_mcp_servers,
    parse_a2a,
    parse_apptainer,
    parse_autonomous,
    parse_claude,
    parse_container,
    parse_context_management,
    parse_extensions,
    parse_health,
    parse_hooks,
    parse_hosts_spec,
    parse_listen,
    parse_proxy,
    parse_restart,
    parse_skills,
    parse_startup,
    parse_startup_commands,
    parse_telegram,
    parse_watchdog,
)
from ._types import AgentConfig, HostsSpec

# Default workdir layout: sac's own state root. Per-agent runtime state
# (CLAUDE.md, .mcp.json, .claude/) lives at
# ``~/.scitex/agent-container/runtime/workspaces/<effective-id>/``. External
# orchestrators that want a different layout can override via ``spec.workdir``.
_DEFAULT_WORKDIR_RUNTIME = "~/.scitex/agent-container/runtime/agents/{name}"

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

# Default workdir for an agent when ``spec.workdir`` is unset. Lives
# under sac's own user-state tree (per the local-state-directories spec):
# ``~/.scitex/agent-container/runtime/workspaces/<name>/`` holds the
# materialized CLAUDE.md, .mcp.json, .claude/ for that agent.
_DEFAULT_WORKDIR_RUNTIME = "~/.scitex/agent-container/runtime/agents/{name}"


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


def _name_from_path(path: Path | str) -> str:
    """Derive the agent name from the YAML path.

    Convention: each agent lives in its own directory
    ``<name>/spec.yaml``. The directory name IS the agent identifier —
    single source of truth. YAMLs do not carry a redundant
    ``metadata.name`` field, and the file is always named ``spec.yaml``.
    """
    return Path(path).parent.name


def _is_relative_path(p: str) -> bool:
    """True when ``p`` is a relative path (not absolute, not ~-prefixed)."""
    return bool(p) and not p.startswith("/") and not p.startswith("~")


def _resolve_python_venv(venv: str | list[str] | None) -> str:
    """Resolve ``spec.python-venv`` to a single venv path on this host.

    Accepts:
      * empty/None: no venv activation (returns "").
      * single string: literal path; must exist or RuntimeError.
        Relative paths (no leading / or ~) are returned as-is and
        resolved at start time relative to the workspace dir on the
        target host — launcher-side existence check is skipped.
      * list of strings: explicit fallback chain — first existing
        absolute/home path wins; relative paths are returned at
        first occurrence (no launcher-side check).
        If none exist/match, raises RuntimeError.

    The fallback chain is intentionally per-agent (in the YAML), not a
    sac-internal default — different agents may want different chains,
    and putting it in the YAML keeps the precedence visible to readers.
    """
    if venv is None or venv == "" or venv == []:
        return ""

    if isinstance(venv, str):
        if _is_relative_path(venv):
            # Relative: defer existence check to target-side launch.
            return venv
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
            if _is_relative_path(candidate):
                # First relative candidate wins immediately (resolved on target).
                return candidate
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


def _parse_env_files(spec: dict) -> list[str]:
    """Parse ``spec.env-file`` into a normalised list of path strings.

    Accepts a string (single file) or a list of strings. Paths are
    stored verbatim; relative paths are resolved at start time relative
    to the workspace dir on the target host.
    """
    raw = spec.get("env-file")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        if not all(isinstance(p, str) for p in raw):
            raise RuntimeError(f"env-file list must contain strings, got: {raw!r}")
        return list(raw)
    raise RuntimeError(
        f"env-file must be a string or list of strings, got "
        f"{type(raw).__name__}: {raw!r}"
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
    # Only sac's own namespace is injected. External consumers (orochi etc.)
    # declare their own env vars explicitly in agent YAML's ``spec.env`` if
    # they want them set.
    auto_env: dict[str, str] = {
        "CLAUDE_AGENT_ID": name,
        "SCITEX_AGENT_CONTAINER_AGENT": name,
    }
    if labels.get("role"):
        auto_env["CLAUDE_AGENT_ROLE"] = labels["role"]
    # v3-realign: model + env + image + mounts live under engine blocks
    # (spec.claude.model, spec.apptainer.{image,binds,env}). The validator
    # rejects the top-level forms; the parsers read the new homes. The
    # top-level AgentConfig.image/model/env/mounts fields are kept for
    # back-compat consumers and populated from the new homes.
    claude_spec = parse_claude(spec)
    apptainer_spec = parse_apptainer(spec)
    model = claude_spec.model or "sonnet"
    display_model = MODEL_DISPLAY_NAMES.get(model, model)
    auto_env["SCITEX_AGENT_CONTAINER_MODEL"] = display_model

    merged_env = {**auto_env, **(apptainer_spec.env or {})}

    # Auto-derive hooks: prepend mkdir for workdir
    hooks = parse_hooks(spec)
    expanded = str(Path(workdir).expanduser())
    mkdir_cmd = f"mkdir -p {expanded}/.claude"
    if mkdir_cmd not in hooks.get("pre_start", []):
        hooks.setdefault("pre_start", []).insert(0, mkdir_cmd)

    # Parse mcp_servers with metadata interpolation (uses effective name)
    mcp_metadata = {**metadata, "name": name}
    mcp_servers = interpolate_mcp_servers(spec.get("mcp_servers", {}), mcp_metadata)

    startup_prompts_raw = spec.get("startup_prompts", []) or []
    startup_prompts = [str(p) for p in startup_prompts_raw if p]

    kind = str(raw.get("kind", "Agent"))
    proxy_spec = parse_proxy(spec, kind=kind)

    return AgentConfig(
        name=name,
        runtime=str(spec.get("runtime") or "apptainer"),
        image=apptainer_spec.image,
        model=model,
        workdir=workdir,
        python_venv=_resolve_python_venv(spec.get("python-venv", "")),
        env=merged_env,
        env_files=_parse_env_files(spec),
        screen_name=screen_name,
        labels=labels,
        container=parse_container(spec),
        claude=claude_spec,
        health=parse_health(spec),
        watchdog=parse_watchdog(spec),
        restart=parse_restart(spec),
        autonomous=parse_autonomous(spec),
        apptainer=apptainer_spec,
        hooks=hooks,
        telegram=parse_telegram(spec),
        skills=parse_skills(spec),
        startup_commands=parse_startup_commands(spec),
        startup_prompts=startup_prompts,
        startup=parse_startup(spec),
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        mcp_servers=mcp_servers,
        multiplexer=spec.get("multiplexer", "tmux"),
        hosts_spec=hosts_spec,
        config_path=str(path),
        user=str(spec.get("user", "")),
        a2a=parse_a2a(spec),
        kind=kind,
        proxy=proxy_spec,
        dot_claude=str(spec.get("dot_claude", "")),
    )
