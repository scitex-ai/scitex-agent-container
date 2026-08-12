"""Config loader for scitex-agent-container/v3 YAML."""

from __future__ import annotations

import re
from pathlib import Path

from ._explicit_validation import validate as _validate_explicit_fields
from ._harness_types import resolve_spec_harness, uses_legacy_harness_key
from ._host import (
    contains_hostname_placeholder,
    resolve_hostname,
    substitute_hostnames,
)
from ._parsers import (
    MODEL_DISPLAY_NAMES,
    interpolate_mcp_servers,
    parse_a2a,
    parse_apptainer,
    parse_autonomous,
    parse_claude,
    parse_comms,
    parse_container,
    parse_context_management,
    parse_extensions,
    parse_health,
    parse_hooks,
    parse_hosts_spec,
    parse_lineage,
    parse_listen,
    parse_proxy,
    parse_restart,
    parse_skills,
    parse_startup_commands,
    parse_watchdog,
)
from ._types import AgentConfig, HostsSpec, StartupCommand

# Guarded default startup command APPENDED to EVERY agent's ``startup_commands``
# (operator directive, Telegram 2862 / card
# ``sac-auto-direnv-allow-at-agent-start-guarded-20260717``). It whitelists a
# project's ``.envrc`` with direnv so the project's NON-SECRET environment
# surfaces inside the container — WITHOUT any per-spec hand-editing, and VISIBLE
# in the materialized spec (``AgentConfig.startup_commands``), not buried in the
# launch code the operator explicitly did not want.
#
# GUARDED + FAIL-SOFT + IDEMPOTENT:
#   * ``command -v direnv`` — no-op when direnv is not installed;
#   * ``[ -f "$PWD/.envrc" ]`` — no-op when the workdir has no ``.envrc``;
#   * trailing ``|| true`` — a failed allow NEVER breaks the boot.
#
# ``$PWD`` is the agent workdir AT RUN TIME: the inner ``bash -lc`` wrapper that
# runs ``startup_commands`` inherits apptainer's ``--pwd
# str(Path(config.workdir).expanduser())`` (runtimes/_apptainer_build_argv.py)
# and sac emits NO ``cd`` before the commands, so ``$PWD`` == the workdir. If a
# workdir is not bound in-container ``$PWD`` falls back to ``$HOME``/``/`` where
# the ``-f "$PWD/.envrc"`` guard simply finds no ``.envrc`` and skips — still
# fail-soft. This surfaces ONLY the project's ``.envrc``; sac SECRETS and
# IDENTITY (SCITEX_TODO_AGENT_ID, cct token pool, listen bearer) stay
# sac-DIRECT-injected and are never routed through direnv.
DEFAULT_DIRENV_ALLOW_COMMAND = (
    'command -v direnv >/dev/null 2>&1 && [ -f "$PWD/.envrc" ] '
    '&& direnv allow "$PWD" || true'
)

# Recognises an already-authored ``direnv allow`` in a startup command so the
# default is not duplicated (idempotency; tolerates extra whitespace).
_DIRENV_ALLOW_RE = re.compile(r"\bdirenv\s+allow\b")


def _with_default_direnv_allow(
    commands: list[StartupCommand],
) -> list[StartupCommand]:
    """Append the guarded direnv-allow default unless one is already present.

    Idempotent: a spec whose ``startup_commands`` ALREADY run ``direnv allow``
    (authored explicitly) is returned unchanged — no duplicate. Otherwise the
    guarded, fail-soft :data:`DEFAULT_DIRENV_ALLOW_COMMAND` is APPENDED so it
    runs last, just before the claude runner ``exec``s. Appended (not
    prepended) so an authored ``startup_commands[0]`` keeps its position.
    """
    for cmd in commands:
        if _DIRENV_ALLOW_RE.search(cmd.command or ""):
            return commands
    return [*commands, StartupCommand(command=DEFAULT_DIRENV_ALLOW_COMMAND)]


# Generic boot-kick used when a spec omits ``startup_prompts``. Role/ID live in
# the auto-generated $HOME/.claude/CLAUDE.md and the task lives on the agent's
# scitex-todo card slice, so the boot prompt only needs a generic kick — per-spec
# restatement of scope/task is the anti-pattern (operator, 2026-06-25). Bare +
# period (no colon) so it also parses plain in YAML without >-/quotes.
DEFAULT_STARTUP_PROMPT = (
    "Start or continue. Scan your scitex-todo card slice, resume any in-flight "
    "or assigned work (hold idle if none), then report readiness. Follow "
    "CLAUDE.md + your skills; don't restate, don't invent scope."
)

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
    # Red-start explicit-fields gate (operator ruling 2026-07-21): every
    # spec field must be WRITTEN — an omitted field is a load error with
    # a complete, paste-ready hint. Runs BEFORE any parsing so an
    # under-specified spec fails with the full field list, not a parser
    # TypeError. No bypass, no migration phase.
    _validate_explicit_fields(raw, path)

    spec = raw.get("spec", {}) or {}
    hosts_spec = parse_hosts_spec(spec)

    # Whole-document ${HOSTNAME} substitution stays multi-host-only
    # (``hosts:`` templates) — env values / command strings in singleton
    # specs may deliberately carry the placeholder for a runtime shell.
    # SINGLETON PLACEMENT is the one exception: ``host: ${HOSTNAME}`` is
    # the portable spelling of "this machine, resolved concretely at load
    # time" (the replacement for the banned ``host: local``; operator
    # directive 2026-07-10), so the placement field alone is substituted.
    is_multi = hosts_spec.hosts != "" and hosts_spec.hosts != []
    singleton_placement_token = not is_multi and contains_hostname_placeholder(
        hosts_spec.host
    )
    hostname = resolve_hostname() if (is_multi or singleton_placement_token) else ""
    if is_multi:
        raw = substitute_hostnames(raw, hostname)
        spec = raw.get("spec", {}) or {}
    elif singleton_placement_token:
        hosts_spec = HostsSpec(
            host=substitute_hostnames(hosts_spec.host, hostname), hosts=""
        )

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
    # Only sac's own namespace is injected. External consumers
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
    # Role-based session-continuity default ("fresh by default, opt-in
    # continue", 2026-06-22). ``claude.session`` now defaults to ``fresh``
    # (parse_claude) so experiment capsules — which carry no coordinator
    # role — start hermetic. But LONG-LIVED coordinator agents
    # (lead/head/worker/telegrammer/project-maintainer/…) must keep their
    # conversation across restarts. Those specs are hand-deployed OUTSIDE
    # this repo and none of them set ``claude.session``, so we map an
    # OMITTED field back to ``continue`` BY ROLE here — the one place that
    # sees both the ``metadata.labels.role`` and the env-injected fleet
    # role. An EXPLICIT ``session:`` (top-level or nested) is authored
    # intent and is left untouched (so ``session: fresh`` on a coordinator
    # stays fresh); a later CLI ``--continue`` / ``--fresh`` still wins by
    # mutating ``config.claude.session`` after load.
    _session_authored = (
        spec.get("session") is not None
        or (spec.get("claude") or {}).get("session") is not None
    )
    if not _session_authored:
        from ._session_continuity import default_session_for_role

        _role = (apptainer_spec.env or {}).get(
            "SCITEX_AGENT_CONTAINER_ROLE"
        ) or labels.get("role")
        claude_spec.session = default_session_for_role(_role)
    model = claude_spec.model or "sonnet"
    display_model = MODEL_DISPLAY_NAMES.get(model, model)
    auto_env["SCITEX_AGENT_CONTAINER_MODEL"] = display_model

    # CLAUDE_AGENT_ACCOUNT — operator #16 self-awareness requirement.
    # Propagate the per-agent account dir-name (e.g. "alpha-example-com")
    # into the container so:
    #   - claude-code-telegrammer enriches its outbound signature with
    #     the live quota for THIS account (PR-A reads this env);
    #   - `sac account quota` keys its quota-cache.json lookup by the
    #     `short` field == this env's first dash-segment;
    #   - the a2a metadata enricher tags every outbound message with
    #     the sender's account + quota for peer back-pressure decisions.
    # Empty string is filtered out so an unpinned (host-shared-OAuth)
    # agent doesn't advertise a misleading account label.
    account_name = str(getattr(claude_spec, "account", "") or "").strip()
    if account_name:
        auto_env["CLAUDE_AGENT_ACCOUNT"] = account_name

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
    if not startup_prompts:
        # DRY default: specs omit startup_prompts and inherit the generic kick.
        startup_prompts = [DEFAULT_STARTUP_PROMPT]
    exclude_hooks = [str(h) for h in (spec.get("exclude_hooks", []) or []) if h]
    exclude_skills = [str(s) for s in (spec.get("exclude_skills", []) or []) if s]

    kind = str(raw.get("kind", "Agent"))
    proxy_spec = parse_proxy(spec, kind=kind)

    # Phase-3 capsule-isolation policy (ADR-0010 Step 2). The
    # ``spec.comms.a2a.listen: false`` toggle is an operator-friendly
    # alias for ``spec.a2a.port: null`` — translate it here so the
    # existing sidecar-disable path (A2ASpec.is_disabled) carries
    # both surfaces without a second code branch downstream.
    comms_spec = parse_comms(spec)
    lineage_spec = parse_lineage(spec)
    a2a_spec = parse_a2a(spec)
    if not comms_spec.a2a.listen:
        a2a_spec = type(a2a_spec)(host=a2a_spec.host, port=None)

    # Builtin sac control plane (operator directive 2026-06-16): EVERY agent
    # gets the sac MCP tools server + the ``server:sac`` push channel so it can
    # communicate (a2a / lineage). Without both, agents can't talk to each other
    # or the lead. Injected by default; opt out per agent with the label
    # ``sac-builtin: "off"``. Idempotent: skips if already declared (the spec's
    # own entry/channel wins). Real wiring still happens downstream — apply_channels
    # (SDK) / tui_channel_config (TUI) for the channel; the .mcp.json / options
    # merge for the tools server.
    _sac_optout = str(labels.get("sac-builtin", "")).strip().lower()
    if _sac_optout not in ("off", "false", "0", "no"):
        if "server:sac" not in {c.strip() for c in claude_spec.channels}:
            claude_spec.channels.append("server:sac")
        if "scitex-agent-container" not in mcp_servers:
            mcp_servers["scitex-agent-container"] = {
                "type": "stdio",
                "command": "/opt/venv-sac/bin/sac",
                "args": ["mcp", "start"],
            }

    return AgentConfig(
        name=name,
        runtime=str(spec.get("runtime") or "tui"),
        # HARNESS — which agent SDK runs the session. NOT
        # spec.claude.provider (the inference backend). ``spec.harness``
        # is canonical; ``spec.provider`` is the deprecated alias, and a
        # STATED disagreement between the two raises rather than picking
        # one silently (config._harness_types).
        harness=resolve_spec_harness(spec),
        harness_key_is_legacy=uses_legacy_harness_key(spec),
        # spec.access REMOVED 2026-06-23 — host access + cwd are declared
        # explicitly via apptainer.binds + spec.workdir (SSoT). A spec still
        # carrying `access:` is rejected loud in _validation.validate_raw.
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
        skills=parse_skills(spec),
        startup_commands=_with_default_direnv_allow(parse_startup_commands(spec)),
        startup_prompts=startup_prompts,
        exclude_hooks=exclude_hooks,
        exclude_skills=exclude_skills,
        context_management=parse_context_management(spec),
        listen=parse_listen(spec),
        extensions=parse_extensions(spec),
        mcp_servers=mcp_servers,
        multiplexer=spec.get("multiplexer", "tmux"),
        hosts_spec=hosts_spec,
        config_path=str(path),
        user=str(spec.get("user", "")),
        a2a=a2a_spec,
        comms=comms_spec,
        lineage=lineage_spec,
        kind=kind,
        proxy=proxy_spec,
        # ADR-0006: default to ``./to_home`` when the key is absent so a
        # ``to_home/`` dir next to spec.yaml auto-discovers. An empty
        # string in YAML keeps the same default behaviour.
        to_home=str(spec.get("to_home", "./to_home") or "./to_home"),
        # ABSENT key -> None ("inherit whatever is on disk", today's implicit
        # cascade). An explicit empty list is NOT the same thing and must not
        # collapse into it: that is a spec saying "inherit NOTHING", which is a
        # legitimate thing for a sandboxed agent to declare. Only `is None`
        # distinguishes them, so the default here cannot be `[]`.
        to_home_layers=_parse_to_home_layers(spec.get("to_home_layers")),
    )


def _parse_to_home_layers(value: object) -> "list[str] | None":
    """Normalise ``spec.to_home_layers`` to a list of names, or ``None``.

    ``None``/absent keeps the implicit cascade. A string is accepted as a
    one-element list, because a single-layer declaration is the common case and
    writing it as a bare scalar in YAML is the obvious thing to do.

    Any other type RAISES. Returning ``None`` for, say, a mapping would make an
    unusable declaration indistinguishable from an absent one — the spec would
    silently fall back to inheriting everything while its author believed it had
    restricted the cascade. That is the exact class of surprise this field
    exists to remove, so it cannot be how the field itself fails.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(
        f"spec.to_home_layers must be a list of layer names (or a single name), "
        f"got {type(value).__name__}: {value!r}. Valid names: "
        f"user-shared, project-shared, per-agent. Omit the key entirely to "
        f"inherit the implicit cascade."
    )
