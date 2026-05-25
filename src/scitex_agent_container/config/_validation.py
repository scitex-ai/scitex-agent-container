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

# v3 ``kind`` discriminator. ``Agent`` = SDK runner (claude_session);
# ``AgentProxy`` = HTTP forwarder (a2a_proxy) with NO SDK. Anything
# else is rejected at parse time.
_VALID_KINDS = frozenset({"Agent", "AgentProxy"})


_SDK_IMAGE = "scitex-agent-container:scitex"


# All spec keys read by load_v3, parsers, or a2a/_server.py.
# Unknown keys are rejected at parse time so typos surface at boot.
# Intentional extension data belongs under spec.extensions.
_KNOWN_SPEC_KEYS = frozenset(
    {
        "runtime",
        "workdir",
        "python-venv",
        "container",
        "screen",  # legacy: agent metadata (screen_name) — no longer drives a multiplexer
        "claude",
        "health",
        "watchdog",
        "restart",
        "hooks",
        "startup_commands",
        "startup_prompts",  # v3-realign: separate from startup_commands (§3)
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
        "proxy",  # AgentProxy upstream forwarder block (kind: AgentProxy only)
        "autonomous",  # F-CS3 — drive-until-done block
        "apptainer",  # F-CS18 — apptainer-specific build extension
        "user",  # container user: "host" | "uid:gid" | "" (image default)
        "to_home",  # ADR-0006 — directory mirrored into container $HOME
        # v3 removed (rejected explicitly below with relocation hints):
        # image (→ spec.apptainer.image), mounts (→ spec.apptainer.binds),
        # env (→ spec.apptainer.env), model (→ spec.claude.model),
        # skills, remote.
    }
)


# v3-realign: top-level fields that moved into engine blocks. Reject
# loudly with a hint pointing to the new home (§3 Removed from v3).
_V3_RELOCATED_FIELDS: dict[str, str] = {
    "image": "spec.apptainer.image",
    "mounts": "spec.apptainer.binds",
    "env": "spec.apptainer.env",
    "model": "spec.claude.model",
}

# v3-realign: fields removed outright (no relocation — different owners).
_V3_REMOVED_FIELDS: dict[str, str] = {
    "skills": (
        "spec.skills is no longer accepted; skills now live under "
        "to_home/.claude/skills/ (§3 Removed)."
    ),
    "dot_claude": (
        "spec.dot_claude is no longer accepted; the dot_claude/ layout "
        "was removed (see ADR-0006). Use spec.to_home and a 'to_home/' dir "
        "next to spec.yaml, with the $HOME-relative layout "
        "to_home/{CLAUDE.md,.mcp.json,.env,.claude/{hooks,skills}}."
    ),
    "remote": (
        "spec.remote is no longer accepted in scitex-agent-container/v3. "
        "Use spec.host: <peer> (singleton on one peer) or "
        "spec.hosts: [peer1, peer2] (multi-instance). "
        'See docs/spec-reference.md "Top-level shape" for the cross-host fields.'
    ),
}


def _validate_provider(provider_block: object) -> list[str]:
    """Validate ``spec.claude.provider`` (vendor backend override).

    Absent / non-dict → no errors (provider feature unused). When the
    block is a dict, both ``base_url`` and ``auth_token_env`` must be
    non-empty strings — an incomplete override would silently fall back
    to Anthropic at runtime, which we refuse to allow.
    """
    if not isinstance(provider_block, dict):
        return []
    errors: list[str] = []
    for field_name in ("base_url", "auth_token_env"):
        val = provider_block.get(field_name)
        if val is None or val == "":
            errors.append(
                f"spec.claude.provider.{field_name} is required and must be "
                "non-empty when spec.claude.provider is declared."
            )
        elif not isinstance(val, str):
            errors.append(
                f"spec.claude.provider.{field_name} must be a string, got "
                f"{type(val).__name__}"
            )
    return errors


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
    if kind not in _VALID_KINDS:
        errors.append(f"kind must be one of {sorted(_VALID_KINDS)}, got '{kind}'")

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
        # v3-realign — fields that moved into engine blocks: reject with
        # a relocation hint so the operator knows the new home.
        for k, new_home in _V3_RELOCATED_FIELDS.items():
            if k in spec:
                errors.append(
                    f"spec.{k} is no longer accepted at the top level; "
                    f"move it to {new_home} (v3 spec realignment §3)."
                )
        # v3-realign — fields removed outright (different owner / shape).
        for k, msg in _V3_REMOVED_FIELDS.items():
            if k in spec:
                errors.append(msg)

        # Unknown spec keys (excluding the v3-relocated/removed set, which
        # already have a more specific message above — listing them as
        # "unknown" would be misleading).
        unknown_spec = (
            set(spec.keys())
            - _KNOWN_SPEC_KEYS
            - set(_V3_RELOCATED_FIELDS)
            - set(_V3_REMOVED_FIELDS)
        )
        for k in sorted(unknown_spec):
            errors.append(
                f"Unknown spec field '{k}'. "
                f"Use spec.extensions for custom data; "
                f"known keys: {sorted(_KNOWN_SPEC_KEYS)}."
            )

        # spec.runtime — sac is apptainer-only since the docker/podman
        # ripout (2026-05-13). Empty/unset is accepted and defaults to
        # apptainer at dispatch.
        runtime = spec.get("runtime")
        if runtime and runtime != "apptainer":
            errors.append(
                f"spec.runtime must be 'apptainer' (got '{runtime}'). "
                "Sac is apptainer-only since 2026-05-13; docker / podman "
                "support was removed for simplicity."
            )

        # spec.image — moved to spec.apptainer.image in v3 (handled by the
        # relocation rejection above). Type-check the new home instead.
        ap_block = spec.get("apptainer", {}) or {}
        ap_image = ap_block.get("image") if isinstance(ap_block, dict) else None
        if ap_image is not None and not isinstance(ap_image, str):
            errors.append(
                f"spec.apptainer.image must be a string, got {type(ap_image).__name__}"
            )

        # spec.dockerfile dropped 2026-05-13 with the docker ripout.
        # Keep type check around for one minor version so explicit
        # use surfaces a clear error rather than silently disappearing.
        dockerfile = spec.get("dockerfile")
        if dockerfile is not None and not isinstance(dockerfile, str):
            errors.append(
                f"spec.dockerfile must be a string, got {type(dockerfile).__name__}"
            )

        # spec.claude.model — F-CS7 (v3: moved from top-level spec.model).
        # Validate against accepted SDK aliases / versioned forms. The
        # SDK silently rejects unknown values (heartbeat fresh, every
        # turn returns 0 tokens), so we surface bad strings at yaml-
        # validate time. Empty / missing is allowed — runtime falls back
        # to its default.
        claude_block = spec.get("claude", {}) or {}
        if not isinstance(claude_block, dict):
            claude_block = {}
        # spec.claude.provider — vendor-agnostic backend override
        # (ProviderSpec). When present, the SDK session runs against an
        # Anthropic-SDK-compatible backend on an API key, so the model id
        # is the provider's own (e.g. 'deepseek-chat') and the claude-*
        # regex below is skipped. Absent → behaviour unchanged.
        provider_block = claude_block.get("provider")
        has_provider = isinstance(provider_block, dict)
        errors.extend(_validate_provider(provider_block))
        model = claude_block.get("model")
        if model is not None:
            if not isinstance(model, str):
                errors.append(
                    f"spec.claude.model must be a string, got {type(model).__name__}"
                )
            elif model and not has_provider and not _VALID_MODEL_RE.match(model):
                errors.append(
                    f"spec.claude.model '{model}' is not an accepted alias. "
                    "Use a bare alias ('opus', 'sonnet', 'haiku', 'inherit', "
                    "'default'), optionally with a context suffix like "
                    "'opus[1m]'; OR the full versioned form "
                    "'claude-<family>-N-M[-<tail>]' (e.g. 'claude-opus-4-7', "
                    "'claude-opus-4-7[1m]', 'claude-haiku-4-5-20251001'). "
                    "Abbreviated forms like 'claude-opus[1m]' are rejected "
                    "by the SDK without raising — every turn returns 0 "
                    "tokens. (When spec.claude.provider is set, the model "
                    "field accepts the provider's own model id instead.)"
                )

        # spec.claude.provider + spec.claude.account are mutually
        # exclusive — an API-key backend needs no OAuth. Declaring both
        # is a config error (the runtime would otherwise have to guess
        # which auth path wins). Reject loudly at validate time.
        if has_provider and (claude_block.get("account") or ""):
            errors.append(
                "spec.claude.provider and spec.claude.account are mutually "
                "exclusive — a provider backend uses an API key, not "
                "Anthropic OAuth. Set exactly one."
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

        # spec.mounts moved to spec.apptainer.binds in v3 — rejected
        # by the relocation block above. No further validation needed.

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

        # kind: AgentProxy coupling rules.
        #
        # AgentProxy has NO SDK — it's a thin HTTP forwarder. So:
        #   * spec.proxy is REQUIRED (no upstream → nothing to forward to)
        #   * spec.claude is IGNORED (no SDK to configure); operator
        #     authoring it is a category error we surface loudly.
        #   * spec.startup_prompts / spec.startup_commands are IGNORED
        #     for the same reason — no SDK to prompt.
        #
        # The mirror also holds for kind: Agent — spec.proxy is rejected
        # there because the SDK runner doesn't read it.
        if kind == "AgentProxy":
            proxy_block = spec.get("proxy")
            if proxy_block is None:
                errors.append(
                    "spec.proxy is required when kind: AgentProxy "
                    "(no upstream to forward to)."
                )
            for forbidden in ("claude", "startup_prompts", "startup_commands"):
                val = spec.get(forbidden)
                if val:
                    errors.append(
                        f"spec.{forbidden} is not allowed when kind: AgentProxy "
                        "(proxy has no SDK to configure / prompt). Remove the field."
                    )
        elif kind == "Agent":
            if "proxy" in spec:
                errors.append(
                    "spec.proxy is only meaningful when kind: AgentProxy; "
                    "remove it for kind: Agent."
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
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        return [f"File not found: {path}"]
    except (
        yaml.YAMLError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        return [f"YAML parse error: {exc}"]

    return validate_raw(raw, str(path))
