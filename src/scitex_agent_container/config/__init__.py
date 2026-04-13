"""YAML config loading and validation for agent definitions.

Public API (backward-compatible with the old single-file config module):
    AgentConfig, load_config, validate_config, resolve_config
    ContainerSpec, ClaudeSpec, HealthSpec, WatchdogSpec, RestartSpec,
    TelegramSpec, OrochiSpec, RemoteSpec, SkillsSpec, StartupCommand
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._loaders import load_v1, load_v2
from ._resolve import resolve_config
from ._types import (
    AgentConfig,
    ClaudeSpec,
    ContainerSpec,
    ContextManagementConfig,
    HealthSpec,
    OrochiSpec,
    RemoteSpec,
    RestartSpec,
    SkillsSpec,
    StartupCommand,
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
    "OrochiSpec",
    "RemoteSpec",
    "RestartSpec",
    "SkillsSpec",
    "StartupCommand",
    "TelegramSpec",
    "WatchdogSpec",
    "load_config",
    "resolve_config",
    "validate_config",
]


def load_config(path: str | Path) -> AgentConfig:
    """Load and validate a YAML config, returning an AgentConfig."""
    path = Path(path).resolve()
    with open(path) as f:
        raw = yaml.safe_load(f)

    errors = validate_raw(raw, str(path))
    if errors:
        raise ValueError(
            f"Config validation failed for {path}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    api_version = raw.get("apiVersion")
    if api_version == "scitex-agent-container/v2":
        return load_v2(raw, path)
    return load_v1(raw, path)
