"""YAML config loading and validation for agent definitions.

Public API:
    AgentConfig, load_config, validate_config, resolve_config
    ContainerSpec, ClaudeSpec, HealthSpec, WatchdogSpec, RestartSpec,
    TelegramSpec, RemoteSpec, SkillsSpec, StartupCommand
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._host import resolve_hostname, substitute_hostnames
from ._loaders import compose_effective_name, load_v3
from ._proxy_types import ProxySpec
from ._resolve import resolve_config
from ._types import (
    AgentConfig,
    ClaudeSpec,
    ContainerSpec,
    ContextManagementConfig,
    HealthSpec,
    HookSpec,
    HostsSpec,
    ListenPort,
    ReadyPattern,
    RemoteSpec,
    RestartSpec,
    SchedulingSpec,
    SkillsSpec,
    StartupCommand,
    StartupSpec,
    TelegramSpec,
    WatchdogSpec,
)
from ._validation import validate_config, validate_raw

__all__ = [
    "AgentConfig",
    "ClaudeSpec",
    "ContainerSpec",
    "ContextManagementConfig",
    "HealthSpec",
    "HookSpec",
    "HostsSpec",
    "ListenPort",
    "ProxySpec",
    "ReadyPattern",
    "RemoteSpec",
    "RestartSpec",
    "SchedulingSpec",
    "SkillsSpec",
    "StartupCommand",
    "StartupSpec",
    "TelegramSpec",
    "WatchdogSpec",
    "compose_effective_name",
    "load_config",
    "resolve_config",
    "resolve_hostname",
    "substitute_hostnames",
    "validate_config",
]


def load_config(path: str | Path) -> AgentConfig:
    """Load and validate a YAML config, returning an AgentConfig.

    Only ``scitex-agent-container/v3`` is accepted. Older apiVersions
    (v1, v2) raise loud validation errors — no backward compatibility.
    """
    path = Path(path).resolve()
    with open(path) as f:
        raw = yaml.safe_load(f)

    errors = validate_raw(raw, str(path))
    if errors:
        raise ValueError(
            f"Config validation failed for {path}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    return load_v3(raw, path)
