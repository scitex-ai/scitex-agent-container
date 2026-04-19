"""YAML config validation."""

from __future__ import annotations

from pathlib import Path

import yaml

_VALID_API_VERSIONS = ("cld-agent/v1", "scitex-agent-container/v2")


def validate_raw(raw: dict, path: str) -> list[str]:
    """Validate raw YAML dict. Returns list of error strings (empty means valid)."""
    errors: list[str] = []

    if not isinstance(raw, dict):
        return [f"Config file is not a YAML mapping: {path}"]

    # apiVersion
    api_version = raw.get("apiVersion")
    if api_version not in _VALID_API_VERSIONS:
        errors.append(
            f"apiVersion must be one of {_VALID_API_VERSIONS}, got '{api_version}'"
        )

    # kind
    kind = raw.get("kind")
    if kind != "Agent":
        errors.append(f"kind must be 'Agent', got '{kind}'")

    # metadata.name
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata is required and must be a mapping")
    elif not metadata.get("name"):
        errors.append("metadata.name is required")

    # spec
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        errors.append("spec is required and must be a mapping")
    else:
        # spec.runtime
        runtime = spec.get("runtime")
        valid_runtimes = ("claude-code", "cursor", "aider", "slurm")
        if runtime and runtime not in valid_runtimes:
            errors.append(
                f"spec.runtime must be one of {valid_runtimes}, got '{runtime}'"
            )

        # container.runtime
        container = spec.get("container", {}) or {}
        cr = container.get("runtime")
        if cr and cr not in ("none", "docker", "apptainer"):
            errors.append(
                f"spec.container.runtime must be none|docker|apptainer, got '{cr}'"
            )

        # container.network
        network = container.get("network")
        if network and network not in ("host", "bridge", "none"):
            errors.append(
                f"spec.container.network must be host|bridge|none, got '{network}'"
            )

        # restart.policy
        restart = spec.get("restart", {}) or {}
        policy = restart.get("policy")
        if policy and policy not in ("never", "on-failure", "always"):
            errors.append(
                f"spec.restart.policy must be never|on-failure|always, got '{policy}'"
            )

        # multiplexer
        mux = spec.get("multiplexer")
        if mux and mux not in ("screen", "tmux"):
            errors.append(f"spec.multiplexer must be 'screen' or 'tmux', got '{mux}'")

        # health.method
        health = spec.get("health", {}) or {}
        method = health.get("method")
        if method and method not in ("multiplexer-alive",):
            errors.append(f"spec.health.method must be 'multiplexer-alive', got '{method}'")

    return errors


def validate_config(path: str | Path) -> list[str]:
    """Validate a config file and return list of errors (empty = valid)."""
    path = Path(path).resolve()
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"File not found: {path}"]
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    return validate_raw(raw, str(path))
