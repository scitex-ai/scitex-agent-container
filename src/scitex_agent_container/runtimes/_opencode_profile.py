"""Materialize one isolated opencode profile from a loaded SAC agent config.

Mirrors ``_hermes_profile`` at pilot scope: deploy ``to_home``, build
the neutral launch plan, compile the credential-free derived
``opencode.json`` via the gateway driver, and write the owner-only env
file carrying the provider key. Secrets never touch argv or the
derived JSON — the profile references the key as ``{env:NAME}`` and
apptainer loads NAME from the ``0600`` env file before serve starts
(the Hermes ``--env-file`` doctrine).

Pilot scope, stated: no MCP translation yet (opencode ``POST /mcp``
dynamic add lands with the pilot), no CCT rail, no session-age GC.
The profile is path-free — serve inherits the container ``--pwd`` —
so no port allocation happens here; the owner argv owns the single
allocation point (explicit ``serve.port`` or a free-loopback pick).
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from ..config import AgentConfig

PROFILE_DIRNAME = ".config/opencode"
PROFILE_FILENAME = "opencode.json"
PROFILE_ENV_FILENAME = "sac.env"


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write an owner-only env file (``0600``, no newlines in values)."""
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Opencode profile env value for {key} contains a newline")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        body = "".join(f"{key}={value}\n" for key, value in values.items())
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _launch_plan(config: AgentConfig):
    """Neutral launch plan with the harness axis flipped to opencode."""
    from ._hermes_profile import _launch_plan as _hermes_launch_plan

    plan = _hermes_launch_plan(config, launch_mode="tui")
    return replace(plan, harness="opencode")


def materialize_opencode_profile(
    config: AgentConfig, *, state_dir: Path
) -> list[Path]:
    """Write the derived ``opencode.json`` + owner-only env to each home."""
    from ._to_home import deploy_to_home
    from ._to_home_overlay import deploy_to_home_overlay, resolve_overlay_upper_home
    from .mcp_config import setup_mcp_config
    from ._apptainer_provider import resolve_provider_api_key
    from ._gateway_opencode import OPENCODE_GATEWAY

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    deploy_to_home(config, str(home))
    setup_mcp_config(config, str(home))
    overlay_home = deploy_to_home_overlay(config)
    targets = [home]
    resolved_upper = resolve_overlay_upper_home(config)
    if overlay_home is not None and resolved_upper is not None:
        setup_mcp_config(config, str(resolved_upper))
        targets.append(resolved_upper)
    plan = _launch_plan(config)
    options = OPENCODE_GATEWAY.parse_agent_options(config)
    rendered = OPENCODE_GATEWAY.compile_profile(
        plan, workdir=str(config.workdir), options=options
    )
    provider_key = resolve_provider_api_key(config)
    env_name = plan.endpoint.auth_env
    profile_env = {env_name: provider_key} if env_name else {}
    for target in targets:
        profile_dir = target / PROFILE_DIRNAME
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / PROFILE_FILENAME).write_text(
            json.dumps(rendered, indent=2), encoding="utf-8"
        )
        _write_env_file(profile_dir / PROFILE_ENV_FILENAME, profile_env)
    return targets


def profile_env_argv(state_dir: Path) -> list[str]:
    """Expose the materialized owner-only opencode env without argv secrets."""
    profile_env = Path(state_dir) / "home" / PROFILE_DIRNAME / PROFILE_ENV_FILENAME
    if not profile_env.is_file():
        raise RuntimeError(
            "Opencode profile env is absent; materialize the opencode workspace "
            f"before building its container argv: {profile_env}"
        )
    mode = profile_env.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(
            f"Opencode profile env {profile_env} has unsafe mode {mode:#o}; "
            "expected no group/world permissions"
        )
    return ["--env-file", str(profile_env)]


def validate_opencode_profile(
    config: AgentConfig, *, state_dir: Path, launch_argv: Sequence[str]
) -> None:
    """Validate the materialized profile against the finalized argv."""
    del launch_argv  # No MCP-pg credential cross-check at pilot scope.
    from ._to_home_overlay import resolve_overlay_upper_home

    targets = [Path(state_dir) / "home"]
    upper = resolve_overlay_upper_home(config)
    if upper is not None:
        targets.append(upper)
    documents: list[dict[str, Any]] = []
    for target in targets:
        path = target / PROFILE_DIRNAME / PROFILE_FILENAME
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(
                f"Opencode profile at {path} is not valid JSON: {exc}"
            ) from exc
        if isinstance(document, dict):
            documents.append(document)
    if not documents:
        raise RuntimeError(
            f"Opencode profile was not materialized for {config.name!r} before launch"
        )
    for document in documents:
        if not document.get("model"):
            raise RuntimeError(
                f"Opencode profile for {config.name!r} names no model; "
                "refusing to launch without a resolved engine"
            )


__all__ = [
    "PROFILE_DIRNAME",
    "PROFILE_ENV_FILENAME",
    "PROFILE_FILENAME",
    "materialize_opencode_profile",
    "profile_env_argv",
    "validate_opencode_profile",
]
