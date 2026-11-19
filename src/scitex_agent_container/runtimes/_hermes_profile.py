"""Materialize one isolated Hermes profile from a loaded SAC agent config."""

from __future__ import annotations

import json
import os
import secrets
import shlex
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import yaml

from ..config import AgentConfig
from ..config._hermes_config import compile_hermes_config
from ..config._hermes_session import hermes_session_key
from ..config._launch_plan import (
    DelegationPolicy,
    Endpoint,
    LaunchPlan,
    ResolvedEngine,
)
from ._apptainer_provider import resolve_provider_api_key
from ._hermes_failover import configure_failover, materialize_pools, resolve_primary_key
from ._hermes_key_probe import preflight_pools
from ._hermes_mcp_env import (  # noqa: F401 (moved; names kept importable here)
    _MCP_NON_SECRET_ENV,
    _MCP_SAC_ENV_REFS,
    _bind_mcp_runtime_env,
    _host_pgpass_source,
    _validate_mcp_pg_credentials,
)
from ._hermes_profile_logs import ensure_hermes_log_files
from ._hermes_skills import materialize_installed_skills
from ._operator_secret_env import profile_operator_secrets
from ._prompt_projection_integrity import resolve_hermes_instruction_projection
from ._to_home import deploy_to_home
from ._to_home_overlay import deploy_to_home_overlay, resolve_overlay_upper_home
from .mcp_config import setup_mcp_config

API_KEY_FILE = "hermes-api.key"
API_PORT_FILE = "hermes-api.port"


def _verified_instruction_text(config: AgentConfig, targets: list[Path]) -> str:
    """Verify every possible home backing projects the same Hermes prompt."""
    resolved = [
        resolve_hermes_instruction_projection(config, target) for target in targets
    ]
    texts = {text for text, _identity in resolved}
    hashes = {identity["sha256"] for _text, identity in resolved}
    if len(texts) != 1 or len(hashes) != 1:
        raise RuntimeError(
            f"Hermes instruction projections disagree across home backings for "
            f"agent {config.name!r}; refusing launch"
        )
    return resolved[0][0]


def validate_hermes_tui_profile(
    config: AgentConfig, *, state_dir: Path, launch_argv: Sequence[str]
) -> None:
    """Validate the materialized profile against the real finalized argv."""
    from ._hermes_sac_runtime import validate_sac_runtime

    validate_sac_runtime(config, launch_argv=launch_argv)
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
    if provider is None:
        raise RuntimeError("Hermes requires the selected engine provider")
    native = str(getattr(provider, "hermes_provider", "") or "").strip()
    base_url = str(provider.base_url or "").rstrip("/")
    if native:
        # Native Hermes provider (e.g. opencode-go): Hermes owns the
        # endpoint, protocol and session handling. SAC only names the
        # provider + model and delivers the key via the agent env.
        if base_url:
            raise RuntimeError("Hermes native provider must not declare base_url")
        endpoint = Endpoint(
            protocol="hermes-native:" + native,
            url="",
            auth_kind="bearer",
            auth_env=str(provider.auth_token_env or ""),
            extra_headers=tuple((getattr(provider, "extra_headers", {}) or {}).items()),
        )
    else:
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
            extra_headers=tuple((getattr(provider, "extra_headers", {}) or {}).items()),
        )
    model = str(config.model or "").strip()
    engine_key = str(config.engine_key or "").strip()
    if not model or not engine_key:
        raise ValueError(
            "Hermes requires a resolved engine model and key; refusing to "
            "materialize a profile with implicit provider/session identity"
        )
    engine = ResolvedEngine(
        key=engine_key,
        model_id=model,
        endpoints=(endpoint,),
        context_window_tokens=config.max_context_tokens,
        reasoning_effort=str(config.reasoning_effort or "") or None,
        upstream_deadline_seconds=config.upstream_deadline_seconds,
        client_abandonment_seconds=config.client_abandonment_seconds,
    )
    session_id = hermes_session_key(config.name, engine_key)
    if str(config.claude.session or "").strip().lower() == "resume":
        session_id = str(config.claude.resume_id or "").strip() or session_id
    return LaunchPlan(
        harness="hermes",
        launch_mode=launch_mode,
        container_backend="apptainer",
        engine=engine,
        endpoint=endpoint,
        may_spawn=config.lineage.may_spawn,
        delegation=DelegationPolicy(
            max_concurrent_children=config.delegation.max_concurrent_children,
            worktree_isolation=config.delegation.worktree_isolation,
        ),
        agent_name=config.name,
        session_id=session_id,
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


def _sac_profile_env(
    config: AgentConfig,
    servers: dict[str, dict[str, Any]],
    *,
    state_dir: Path | None = None,
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
    env = {
        "SAC_LISTEN_BASE_URL": listen_base_url(),
        "SAC_LISTEN_BEARER": bearer,
        "SAC_NAME": config.name,
    }
    # Per-agent caller credential beside the host-wide bearer (see
    # _listen.agent_tokens). STAGED only: the profile is also materialised by
    # dry-run paths, so the real launch gate is what activates it.
    if state_dir is not None:
        from .._listen.agent_tokens import AGENT_TOKEN_ENV, stage_agent_token

        env[AGENT_TOKEN_ENV] = stage_agent_token(state_dir)
    return env


def _cct_profile_env(home: Path) -> dict[str, str]:
    """Mirror the CCT token + agent id into the Hermes profile env.

    ``deploy_to_home`` wrote them to ``home/.env`` (the container
    ``--env-file``); Hermes' tool-child secret scope reads only
    ``<HERMES_HOME>/.env``. Returns {} when the rail is not materialised
    (values never logged).
    """
    from ._secret_pool import _read_env_file

    try:
        materialised = _read_env_file(home / ".env")
    except OSError:
        return {}
    out: dict[str, str] = {}
    token = (materialised.get("CCT_BOT_TOKEN") or "").strip()
    if not token:
        return {}
    out["CCT_BOT_TOKEN"] = token
    agent_id = (materialised.get("CCT_AGENT_ID") or "").strip()
    if agent_id:
        out["CCT_AGENT_ID"] = agent_id
    return out


def _write_profile_env(path: Path, values: dict[str, str]) -> None:
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Hermes profile env value for {key} contains a newline")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # Quote values carrying whitespace: apptainer --env-file is
        # word-split at launch, so an unquoted "SciTeX Scholar" executes
        # "Scholar" as a command and the agent FATALs before it boots
        # (scitex-dev, 2026-10-22). shlex.quote is the inverse of the
        # evaluation the launcher performs — plain values pass through
        # unchanged, only the dangerous ones gain quotes.
        body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
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


def _verified_route(config: AgentConfig, home: Path, *, launch_mode: str):
    """Probe before deploying files into an existing agent home."""
    from .._lifecycle._hermes_restart_preflight import (
        consume_prepared_route,
        prepare_hermes_successor,
    )

    prepared = consume_prepared_route(config, launch_mode=launch_mode)
    policy = getattr(config, "hermes_failover", None)
    if (
        prepared is None
        and policy is not None
        and (getattr(policy, "accounts", None) or getattr(policy, "engines", None))
    ):
        targets = [home / ".hermes"]
        upper = resolve_overlay_upper_home(config)
        if upper is not None:
            targets.append(upper / ".hermes")
        prepared = prepare_hermes_successor(config, profiles=targets)
    if prepared is not None:
        config._hermes_prepared_provider_key = prepared.provider_key
        return prepared.plan, prepared.selection, prepared.env, prepared.pools
    plan = _launch_plan(config, launch_mode=launch_mode)
    selection = compile_hermes_config(plan, workdir=str(config.workdir))
    env, pools = configure_failover(config, selection)
    targets = [home / ".hermes"]
    upper = resolve_overlay_upper_home(config)
    if upper is not None:
        targets.append(upper / ".hermes")
    preflight_pools(selection, pools, targets)
    if pools:
        selection["sac_managed_model"] = True
    return plan, selection, env, pools


def _apply_verified_route(rendered: dict, selection: dict) -> None:
    for key in (
        "model",
        "providers",
        "fallback_providers",
        "credential_pool_strategies",
        "sac_managed_model",
    ):
        if key in selection:
            rendered[key] = selection[key]
    if selection.get("credential_pool_strategies"):
        # A stale base alias must not bypass the managed credential pool in
        # main or auxiliary clients. The pool owns the live usable key.
        for provider in rendered.get("providers", {}).values():
            provider["key_env"] = ""
            provider.pop("api_key", None)
    judge = rendered.get("auxiliary", {}).get("goal_judge")
    verified = selection.get("model")
    verified_default = verified.get("default") if isinstance(verified, dict) else None
    verified_provider = verified.get("provider") if isinstance(verified, dict) else None
    actual_provider = rendered.get("model", {}).get("provider")
    actual = rendered.get("model", {}).get("default")
    if (
        judge
        and verified_default is not None
        and verified_provider is not None
        and actual is not None
        and actual_provider is not None
        and (
            judge.get("model") != verified_default
            or judge.get("provider") != verified_provider
        )
    ):
        # Operator rule (prompt-2026-1022): the goal judge rides the agent's
        # verified lane — same provider AND same model the agent itself runs
        # on, never a Hermes default, never deepseek, never a stale lane the
        # failover just abandoned. A plausible judge on a dead lane still
        # pauses every goal after 5 transport failures.
        from .._lifecycle._hermes_restart_preflight import same_goal_model

        if not same_goal_model(judge.get("model"), verified_default):
            raise ValueError(
                "Hermes verified fallback changes the authored goal judge model"
            )
        judge["provider"] = verified_provider
        judge["model"] = verified_default


def _profile_primary_key(config):
    prepared = getattr(config, "_hermes_prepared_provider_key", None)
    if prepared is not None:
        del config._hermes_prepared_provider_key
        return prepared
    return resolve_primary_key(config, resolve_provider_api_key)


def materialize_hermes_profile(
    config: AgentConfig, *, state_dir: Path, api_port: int
) -> tuple[str, list[Path]]:
    """Write matching Hermes profiles to every possible container-home backing."""
    state_dir.mkdir(parents=True, exist_ok=True)
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    plan, selection, failover_env, credential_pools = _verified_route(
        config, home, launch_mode="headless"
    )
    deploy_to_home(config, str(home))
    setup_mcp_config(config, str(home))
    overlay_home = deploy_to_home_overlay(config)
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        setup_mcp_config(config, str(resolved_upper))
        targets.append(resolved_upper)
    system_prompt = _verified_instruction_text(config, targets)
    skill_dirs = materialize_installed_skills(
        config, [target / ".hermes" for target in targets]
    )
    api_key = ensure_api_key(state_dir)
    provider_key = _profile_primary_key(config)
    rendered = compile_hermes_config(
        plan,
        workdir=str(config.workdir),
        run_budget_seconds=config.hermes_run_budget_seconds,
        max_turns=config.hermes_max_turns,
        goals=config.hermes_goals,
        approval_mode="off",
        compression=config.hermes_compression,
        background_review=config.hermes_background_review,
        system_prompt=system_prompt,
    )
    _apply_verified_route(rendered, selection)
    rendered.setdefault("skills", {})["external_dirs"] = skill_dirs
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
    from ._pg_identity_credentials import materialize_project_pgpass

    materialize_project_pgpass(config, home_backings=targets, servers=servers)
    env_name = plan.endpoint.auth_env
    profile_env = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(api_port),
        "API_SERVER_KEY": api_key,
        env_name: provider_key,
        **_sac_profile_env(config, servers, state_dir=state_dir),
        **cct_env,
        **profile_operator_secrets(config),
        **failover_env,
    }
    if credential_pools and plan.endpoint.auth_env not in failover_env:
        profile_env.pop(plan.endpoint.auth_env, None)
    from .._sandbox import seal_profile_env

    profile_env = seal_profile_env(config, profile_env, auth_env=plan.endpoint.auth_env)
    for target in targets:
        profile = target / ".hermes"
        profile.mkdir(parents=True, exist_ok=True)
        ensure_hermes_log_files(profile)
        from ._hermes_hook_config import preserve_hook_config

        (profile / "config.yaml").write_text(
            yaml.safe_dump(preserve_hook_config(profile, rendered), sort_keys=False),
            encoding="utf-8",
        )
        _write_profile_env(profile / ".env", profile_env)
        materialize_pools(profile, credential_pools)
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
    plan, selection, failover_env, credential_pools = _verified_route(
        config, home, launch_mode="tui"
    )
    if deploy_home:
        deploy_to_home(config, str(home))
        overlay_home = deploy_to_home_overlay(config)
    else:
        overlay_home = resolve_overlay_upper_home(config)
    setup_mcp_config(config, str(home))
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        setup_mcp_config(config, str(resolved_upper))
        targets.append(resolved_upper)
    system_prompt = _verified_instruction_text(config, targets)
    provider_key = _profile_primary_key(config)
    skill_dirs = materialize_installed_skills(
        config, [target / ".hermes" for target in targets]
    )
    rendered = compile_hermes_config(
        plan,
        workdir=str(config.workdir),
        run_budget_seconds=config.hermes_run_budget_seconds,
        max_turns=config.hermes_max_turns,
        goals=config.hermes_goals,
        approval_mode="off",
        compression=config.hermes_compression,
        background_review=config.hermes_background_review,
        system_prompt=system_prompt,
    )
    _apply_verified_route(rendered, selection)
    servers, eager_toolsets = _mcp_servers(
        home, channels=getattr(config.claude, "channels", None)
    )
    rendered.setdefault("skills", {})["external_dirs"] = skill_dirs
    if servers:
        _bind_mcp_runtime_env(config, servers)
    from ._hermes_cct import wire_hermes_cct_rail

    cct_env = wire_hermes_cct_rail(config, home=home, servers=servers)
    if servers:
        rendered["mcp_servers"] = servers
    if eager_toolsets:
        rendered["toolsets"] = [*rendered.get("toolsets", []), *eager_toolsets]
    from ._pg_identity_credentials import materialize_project_pgpass

    materialize_project_pgpass(config, home_backings=targets, servers=servers)
    profile_env = {
        plan.endpoint.auth_env: provider_key,
        **_sac_profile_env(config, servers, state_dir=state_dir),
        **cct_env,
        # CCT rail: Hermes resolves tool-child env through the profile
        # secret scope, which is built from <HERMES_HOME>/.env — i.e. THIS
        # file — not from home/.env where deploy_to_home wrote the token.
        # Mirror the token + agent id here so terminal.env_passthrough can
        # actually resolve them (measured 2026-09-24: apps-lead Bash tool
        # reported TOKEN-LEN=0 with the value present in home/.env and the
        # Hermes process env but absent from the scope file).
        **_cct_profile_env(home),
        **profile_operator_secrets(config),
        **failover_env,
    }
    if credential_pools and plan.endpoint.auth_env not in failover_env:
        profile_env.pop(plan.endpoint.auth_env, None)
    from .._sandbox import seal_profile_env

    profile_env = seal_profile_env(config, profile_env, auth_env=plan.endpoint.auth_env)
    for target in targets:
        profile = target / ".hermes"
        profile.mkdir(parents=True, exist_ok=True)
        ensure_hermes_log_files(profile)
        from ._hermes_hook_config import preserve_hook_config

        (profile / "config.yaml").write_text(
            yaml.safe_dump(preserve_hook_config(profile, rendered), sort_keys=False),
            encoding="utf-8",
        )
        _write_profile_env(profile / ".env", profile_env)
        materialize_pools(profile, credential_pools)
    return targets


__all__ = [
    "API_KEY_FILE",
    "API_PORT_FILE",
    "ensure_api_key",
    "materialize_hermes_profile",
    "materialize_hermes_tui_profile",
    "validate_hermes_tui_profile",
]
