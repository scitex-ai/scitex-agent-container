"""YAML config validation."""

from __future__ import annotations

from pathlib import Path

import yaml

_VALID_API_VERSIONS = ("scitex-agent-container/v3",)


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

    # metadata (optional dict — agent name comes from parent dir, not from
    # metadata.name; the field is no longer accepted)
    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append("metadata, if present, must be a mapping")
    elif isinstance(metadata, dict) and "name" in metadata:
        errors.append(
            "metadata.name is no longer accepted; the agent name is "
            "derived from the parent directory (dir-as-SSoT). Remove "
            "the metadata.name field and ensure the YAML lives at "
            "<name>/<name>.yaml."
        )

    # spec
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        errors.append("spec is required and must be a mapping")
    else:
        # spec.runtime
        runtime = spec.get("runtime")
        valid_runtimes = ("claude-code", "cursor", "aider", "slurm", "slurm-tenant")
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

        # container.mount_host_claude (opt-in; default False)
        mhc = container.get("mount_host_claude")
        if mhc is not None and not isinstance(mhc, bool):
            errors.append(
                "spec.container.mount_host_claude must be a boolean, got "
                f"{type(mhc).__name__}"
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
            errors.append(
                f"spec.health.method must be 'multiplexer-alive', got '{method}'"
            )

        # host / hosts (mutually exclusive)
        has_host = "host" in spec
        has_hosts = "hosts" in spec
        if has_host and has_hosts:
            errors.append(
                "spec.host and spec.hosts are mutually exclusive — set "
                "exactly one (host: singleton, hosts: multi-instance)"
            )
        if has_host:
            host_val = spec.get("host")
            if host_val is not None and not isinstance(host_val, (str, list)):
                errors.append(
                    f"spec.host must be a string, list of strings, or empty; "
                    f"got {type(host_val).__name__}"
                )
            elif isinstance(host_val, list) and not all(
                isinstance(h, str) for h in host_val
            ):
                errors.append("spec.host list must contain only strings")
        if has_hosts:
            hosts_val = spec.get("hosts")
            if hosts_val is None:
                errors.append(
                    "spec.hosts cannot be empty — use 'all' (every fleet "
                    "host) or a list of host names"
                )
            elif isinstance(hosts_val, str) and hosts_val != "all":
                errors.append(f"spec.hosts string must be 'all', got '{hosts_val}'")
            elif isinstance(hosts_val, list) and not all(
                isinstance(h, str) for h in hosts_val
            ):
                errors.append("spec.hosts list must contain only strings")
            elif not isinstance(hosts_val, (str, list)):
                errors.append(
                    f"spec.hosts must be 'all' or a list of strings; "
                    f"got {type(hosts_val).__name__}"
                )

        # Reject the old `scheduling:` block — replaced by host/hosts.
        if "scheduling" in spec:
            errors.append(
                "spec.scheduling block is no longer accepted. Use spec.host "
                "(singleton, optionally with fallback list) or spec.hosts "
                "(multi-instance, 'all' or list)."
            )

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
