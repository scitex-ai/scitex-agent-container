"""YAML config validation.

Sac is SDK-only and container-only since the CLI/TUI runtime cleanup.
Accepted ``spec.runtime`` values are ``docker``, ``podman``, ``apptainer``
— each backend wraps the same long-running Claude Agent SDK runner.
Communication with the agent uses the HTTP A2A surface, never panes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# Accepted shapes for ``spec.model`` (F-CS7).
#
# claude-agent-sdk silently rejects unknown aliases — the runner stays
# alive, the heartbeat is fresh, but every turn returns 0 input tokens
# and 0 output tokens because the SDK never makes the API call. Pin
# the validation here so the failure surfaces at yaml-validate time
# instead of as a hung-looking agent.
#
# Two acceptable shapes:
#   1. Bare alias: ``opus`` / ``sonnet`` / ``haiku`` / ``inherit`` /
#      ``default``, optionally with a context-suffix (``[1m]``).
#   2. Full versioned form: ``claude-<family>-N-M`` with optional date
#      tail (``-20251001``) and optional context-suffix.
#
# Reproduction (2026-05-05): ``claude-opus[1m]`` (abbreviated, missing
# the version digits) was accepted by the YAML loader but silently
# rejected by the SDK — every turn returned ``input_tokens=0``,
# ``output_tokens=0``, ``iterations=[]``. Other peers using
# ``claude-opus-4-7[1m]`` worked fine.
_VALID_MODEL_RE = re.compile(
    r"""
    ^(?:
        (?:opus|sonnet|haiku|inherit|default)
        |
        claude-(?:opus|sonnet|haiku)-\d+-\d+(?:-[a-z0-9]+)*
    )
    (?:\[[a-zA-Z0-9_]+\])?
    $
    """,
    re.VERBOSE,
)

_VALID_API_VERSIONS = ("scitex-agent-container/v3",)

_KNOWN_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "metadata", "spec"})


_SDK_IMAGE = "scitex-agent-container:scitex"


# All spec keys read by load_v3, parsers, or a2a/_server.py.
# Unknown keys are rejected at parse time so typos surface at boot.
# Intentional extension data belongs under spec.extensions.
_KNOWN_SPEC_KEYS = frozenset(
    {
        "runtime",
        "image",
        "dockerfile",
        "model",
        "workdir",
        "python-venv",
        "env",
        "container",
        "screen",  # legacy: agent metadata (screen_name) — no longer drives a multiplexer
        "claude",
        "health",
        "watchdog",
        "restart",
        "hooks",
        "telegram",
        "remote",
        "skills",
        "startup_commands",
        "startup",
        "context_management",
        "listen",
        "extensions",
        "mcp_servers",
        "host",
        "hosts",
        "session",  # shortcut alias for spec.claude.session
        "scheduling",  # rejected with a specific actionable message below
        "a2a",  # A2A sidecar config read by a2a/_server.py
        "autonomous",  # F-CS3 — drive-until-done block
        "apptainer",  # F-CS18 — apptainer-specific build extension
        "mounts",  # declarative bind-mounts: list of {src, dst, mode?}
        "user",  # container user: "host" | "uid:gid" | "" (image default)
    }
)


def validate_raw(raw: dict, path: str) -> list[str]:
    """Validate raw YAML dict. Returns list of error strings (empty means valid)."""
    errors: list[str] = []

    if not isinstance(raw, dict):
        return [f"Config file is not a YAML mapping: {path}"]

    # Unknown top-level keys
    unknown_top = set(raw.keys()) - _KNOWN_TOP_LEVEL_KEYS
    for k in sorted(unknown_top):
        errors.append(
            f"Unknown top-level field '{k}'. "
            f"Valid keys: {sorted(_KNOWN_TOP_LEVEL_KEYS)}."
        )

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
        # Unknown spec keys
        unknown_spec = set(spec.keys()) - _KNOWN_SPEC_KEYS
        for k in sorted(unknown_spec):
            errors.append(
                f"Unknown spec field '{k}'. "
                f"Use spec.extensions for custom data; "
                f"known keys: {sorted(_KNOWN_SPEC_KEYS)}."
            )

        # spec.runtime — sac is SDK-only; the only accepted values are
        # the container backends. Each wraps the same long-running
        # Claude Agent SDK runner with a different permission model.
        runtime = spec.get("runtime")
        valid_runtimes = ("docker", "podman", "apptainer")
        if runtime and runtime not in valid_runtimes:
            errors.append(
                f"spec.runtime must be one of {valid_runtimes}, got '{runtime}'. "
                "Sac is SDK-only since the CLI/TUI cleanup; pick a container "
                f"backend and image (default {_SDK_IMAGE})."
            )

        # spec.image (F-CS16 phase 2a) — top-level container image tag.
        # Empty string is allowed and falls back to the default at
        # dispatch time. Type check only here.
        image = spec.get("image")
        if image is not None and not isinstance(image, str):
            errors.append(f"spec.image must be a string, got {type(image).__name__}")

        # spec.dockerfile (F-CS16 phase 2a) — host-relative path to a
        # Dockerfile sac auto-builds when ``image`` is missing locally
        # (phase 2d wires the build). Type check only.
        dockerfile = spec.get("dockerfile")
        if dockerfile is not None and not isinstance(dockerfile, str):
            errors.append(
                f"spec.dockerfile must be a string, got {type(dockerfile).__name__}"
            )

        # spec.model — F-CS7: validate against accepted SDK aliases /
        # versioned forms. The SDK silently rejects unknown values
        # (heartbeat fresh, every turn returns 0 tokens), so we surface
        # bad strings at yaml-validate time. Empty / missing is allowed
        # — runtime falls back to its default.
        model = spec.get("model")
        if model is not None:
            if not isinstance(model, str):
                errors.append(
                    f"spec.model must be a string, got {type(model).__name__}"
                )
            elif model and not _VALID_MODEL_RE.match(model):
                errors.append(
                    f"spec.model '{model}' is not an accepted alias. "
                    "Use a bare alias ('opus', 'sonnet', 'haiku', 'inherit', "
                    "'default'), optionally with a context suffix like "
                    "'opus[1m]'; OR the full versioned form "
                    "'claude-<family>-N-M[-<tail>]' (e.g. 'claude-opus-4-7', "
                    "'claude-opus-4-7[1m]', 'claude-haiku-4-5-20251001'). "
                    "Abbreviated forms like 'claude-opus[1m]' are rejected "
                    "by the SDK without raising — every turn returns 0 "
                    "tokens."
                )

        # container.runtime
        container = spec.get("container", {}) or {}
        cr = container.get("runtime")
        if cr and cr not in ("none", "docker", "podman", "apptainer"):
            errors.append(
                f"spec.container.runtime must be none|docker|podman|apptainer, got '{cr}'"
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

        # health.method — sole supported probe is the SDK runner's
        # /healthz / heartbeat-file check (see runtimes/_sdk_common.py).
        health = spec.get("health", {}) or {}
        method = health.get("method")
        if method and method not in ("sdk-alive",):
            errors.append(f"spec.health.method must be 'sdk-alive', got '{method}'")

        # spec.mounts — declarative bind-mounts. Each entry: {src, dst, mode?}.
        # `src` and `dst` are strings (host path / container path); `mode` is
        # optional and one of "rw" (default) / "ro".
        mounts = spec.get("mounts")
        if mounts is not None:
            if not isinstance(mounts, list):
                errors.append(
                    f"spec.mounts must be a list of mappings, got "
                    f"{type(mounts).__name__}"
                )
            else:
                for i, m in enumerate(mounts):
                    if not isinstance(m, dict):
                        errors.append(
                            f"spec.mounts[{i}] must be a mapping, got "
                            f"{type(m).__name__}"
                        )
                        continue
                    extra = set(m.keys()) - {"src", "dst", "mode"}
                    for k in sorted(extra):
                        errors.append(
                            f"spec.mounts[{i}]: unknown key '{k}'. "
                            "Valid keys: src, dst, mode."
                        )
                    src = m.get("src")
                    dst = m.get("dst")
                    if not isinstance(src, str) or not src:
                        errors.append(
                            f"spec.mounts[{i}].src must be a non-empty string"
                        )
                    if not isinstance(dst, str) or not dst:
                        errors.append(
                            f"spec.mounts[{i}].dst must be a non-empty string"
                        )
                    mode = m.get("mode")
                    if mode is not None and mode not in ("rw", "ro"):
                        errors.append(
                            f"spec.mounts[{i}].mode must be 'rw' or 'ro', got '{mode}'"
                        )

        # spec.user — container user. Three accepted shapes:
        #   * ""              (default) → image's USER (typically `agent`)
        #   * "host"          → run as host operator's UID:GID
        #   * "<uid>:<gid>"   → explicit numeric, e.g. "1000:1000"
        # Pair with spec.mounts and (optionally) spec.env.HOME to give an
        # agent host-shaped paths + ownership without any special flags.
        user_val = spec.get("user")
        if user_val is not None:
            if not isinstance(user_val, str):
                errors.append(
                    f"spec.user must be a string, got {type(user_val).__name__}"
                )
            elif user_val and user_val != "host" and ":" not in user_val:
                errors.append(
                    f'spec.user must be "", "host", or "<uid>:<gid>"; '
                    f"got '{user_val}'"
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

        # spec.autonomous (F-CS3 phase 1) — drive-until-done.
        autonomous = spec.get("autonomous")
        if autonomous is not None:
            if not isinstance(autonomous, dict):
                errors.append(
                    "spec.autonomous must be a mapping; got "
                    f"{type(autonomous).__name__}"
                )
            else:
                drive_until = autonomous.get("drive_until")
                if drive_until is not None and not isinstance(drive_until, str):
                    errors.append("spec.autonomous.drive_until must be a string")
                elif drive_until == "":
                    errors.append("spec.autonomous.drive_until must be non-empty")
                for fld in ("max_turns", "idle_kick_after_s"):
                    val = autonomous.get(fld)
                    if val is not None:
                        if not isinstance(val, int) or isinstance(val, bool):
                            errors.append(f"spec.autonomous.{fld} must be an integer")
                        elif val <= 0:
                            errors.append(f"spec.autonomous.{fld} must be > 0")
                kick = autonomous.get("kick_text")
                if kick is not None and not isinstance(kick, str):
                    errors.append("spec.autonomous.kick_text must be a string")
                enabled = autonomous.get("enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    errors.append("spec.autonomous.enabled must be a boolean")

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
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        return [f"File not found: {path}"]
    except (
        yaml.YAMLError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        return [f"YAML parse error: {exc}"]

    return validate_raw(raw, str(path))
