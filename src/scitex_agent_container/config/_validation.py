"""YAML config validation."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

# F-CS6 — yaml-field rename for ``spec.runtime``.
#
# The internal codebase still keys dispatch on the original names
# (``claude-code`` / ``claude-session``), so the new aliases are
# normalised back to the canonical form at load time. A stderr
# warning is emitted once per shell session per renamed value so
# stale yamls keep working without a constant log nag.
#
# §5 of the scitex CLI conventions mandates HARD redirects, but that
# rule governs CLI commands; yaml field values can't be atomically
# rewritten across every host's checked-in agent definitions, so a
# soft alias is the right level of breakage here.
_RUNTIME_RENAMES = {
    "claude-cli-tui": "claude-code",
    "claude-sdk-persistent": "claude-session",
}


def _runtime_alias_warn_marker(old_name: str) -> Path:
    """One marker file per shell session per renamed value.

    Keying on PPID gives one warning per *interactive shell* — child
    invocations from the same shell don't re-print. Matches the
    pattern documented in scitex/general/03_interface_02_cli/
    11_deprecation.md §5a.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    user = os.environ.get("USER", "u")
    ppid = os.environ.get("PPID", "0")
    return Path(runtime_dir) / f"sac-runtime-rename-{user}-{ppid}-{old_name}.flag"


def normalize_runtime(value: str | None) -> str | None:
    """Return the canonical runtime value; warn on first use of an alias.

    Accepts the new yaml-friendly aliases (``claude-cli-tui``,
    ``claude-sdk-persistent``) and returns the long-standing internal
    names (``claude-code``, ``claude-session``). Unknown / canonical
    values pass through unchanged. ``None`` becomes ``None``.
    """
    if value is None:
        return None
    canonical = _RUNTIME_RENAMES.get(value)
    if canonical is None:
        return value
    marker = _runtime_alias_warn_marker(value)
    if not marker.exists():
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:  # stx-allow: fallback (reason: marker is best-effort; missing /tmp shouldn't block load)
            pass
        sys.stderr.write(
            f"warning: spec.runtime: '{value}' is the new alias for "
            f"'{canonical}'. Both work; the alias will become canonical "
            "in a future major release. (F-CS6)\n"
        )
        sys.stderr.flush()
    return canonical


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


def _legacy_runtime_redirect(old: str, new: str, image: str) -> str:
    """Render the §5-style hard-error message for a renamed runtime.

    Each legacy value names a specific replacement so a stale yaml
    gets fixed in one pass:

      spec.runtime: claude-session   ->   spec.runtime: docker
                                          spec.image:   scitex-agent-container:sdk-persistent
                                          spec.dockerfile: ./containers/Dockerfile.sdk-persistent
    """
    return (
        f"spec.runtime: '{old}' was renamed to spec.runtime: '{new}'. "
        f"Set:\n"
        f"  spec.runtime:    {new}\n"
        f"  spec.image:      {image}\n"
        f"  spec.dockerfile: ./containers/Dockerfile.sdk-persistent\n"
        "(F-CS16 phase 2e: sac is container-only; the old runtime "
        "names are no longer accepted.)"
    )


# F-CS16 phase 2e — every legacy runtime value hard-errors with a
# redirect that names the new shape. Mapping kept module-level so
# tests can pin individual messages without re-deriving the dict.
_SDK_IMAGE = "scitex-agent-container:sdk-persistent"


def legacy_runtime_redirect_message(runtime: str) -> str | None:
    """Return the §5-style redirect text for a legacy runtime, or None.

    Phase 2e.1 callers (lifecycle, dispatch helpers, error reporters)
    use this to surface "use ``runtime: docker`` + image + dockerfile"
    guidance even while the validator still accepts the legacy value.
    F-CS17's sweep flips the validator itself to call this function
    and append the result to ``errors``.
    """
    return _LEGACY_RUNTIME_REDIRECTS.get(runtime)


_LEGACY_RUNTIME_REDIRECTS = {
    "claude-session": _legacy_runtime_redirect("claude-session", "docker", _SDK_IMAGE),
    "claude-sdk-persistent": _legacy_runtime_redirect(
        "claude-sdk-persistent", "docker", _SDK_IMAGE
    ),
    "claude-code": (
        "spec.runtime: 'claude-code' (CLI/TUI runtime) is no longer "
        "supported by sac. The CLI/TUI path was removed in F-CS17. "
        "Use the SDK runner instead:\n"
        "  spec.runtime:    docker\n"
        f"  spec.image:      {_SDK_IMAGE}\n"
        "  spec.dockerfile: ./containers/Dockerfile.sdk-persistent"
    ),
    "claude-cli-tui": (
        "spec.runtime: 'claude-cli-tui' is no longer supported "
        "(CLI/TUI runtime removed in F-CS17). Use:\n"
        "  spec.runtime:    docker\n"
        f"  spec.image:      {_SDK_IMAGE}\n"
        "  spec.dockerfile: ./containers/Dockerfile.sdk-persistent"
    ),
    "slurm": (
        "spec.runtime: 'slurm' is no longer supported. Sac is a "
        "container wrapper; HPC scheduling is the operator's "
        "concern (submit your own sbatch and run 'sac agent start' "
        "inside the allocation). See F-CS16 design doc."
    ),
    "slurm-tenant": (
        "spec.runtime: 'slurm-tenant' is no longer supported (see "
        "the redirect for 'slurm'). Submit sbatch yourself and "
        "invoke 'sac agent start' inside the allocation."
    ),
}

# All spec keys read by load_v3, parsers, or a2a/_server.py.
# Unknown keys are rejected at parse time so typos surface at boot.
# Intentional extension data belongs under spec.extensions.
_KNOWN_SPEC_KEYS = frozenset(
    {
        "runtime",
        "image",  # F-CS16 phase 2a — flattened from spec.container.image
        "dockerfile",  # F-CS16 phase 2a — auto-build source when image missing
        "model",
        "workdir",
        "python-venv",
        "env",
        "screen",
        "container",
        "claude",
        "health",
        "watchdog",
        "restart",
        "hooks",
        "telegram",
        "remote",
        "slurm",
        "skills",
        "startup_commands",
        "startup",
        "context_management",
        "listen",
        "extensions",
        "mcp_servers",
        "multiplexer",
        "host",
        "hosts",
        "session",  # shortcut alias for spec.claude.session
        "scheduling",  # rejected with a specific actionable message below
        "a2a",  # A2A sidecar config read by a2a/_server.py
        "orochi",  # Orochi-specific extension namespace
        "autonomous",  # F-CS3 — drive-until-done block
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

        # spec.runtime — F-CS17 stage 2.
        #
        # The migration's grace period (phase 2e.1) is over. Every
        # legacy value now hard-errors with the redirect string from
        # ``legacy_runtime_redirect_message`` — see the §5-style
        # guidance there. Canonical engines (docker / podman /
        # apptainer) remain the only accepted values.
        runtime = spec.get("runtime")
        valid_runtimes = ("docker", "podman", "apptainer")
        legacy_msg = legacy_runtime_redirect_message(runtime or "")
        if legacy_msg is not None:
            errors.append(legacy_msg)
        elif runtime and runtime not in valid_runtimes:
            errors.append(
                f"spec.runtime must be one of {valid_runtimes}, got '{runtime}'"
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
