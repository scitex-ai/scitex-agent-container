"""YAML config validation.

Sac is SDK-only and container-only since the CLI/TUI runtime cleanup.
Accepted ``spec.runtime`` values are ``docker``, ``podman``, ``apptainer``
— each backend wraps the same long-running Claude Agent SDK runner.
Communication with the agent uses the HTTP A2A surface, never panes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._acl_validation import validate_phase3_acl

# Cohesive validation blocks extracted into sibling modules to keep this
# orchestrator under the 512-line cap (same pattern as _acl_validation /
# _provider_validation). ``_VALID_MODEL_RE`` is re-exported here for
# back-compat with any importer of the old ``_validation._VALID_MODEL_RE``.
from ._claude_validation import _VALID_MODEL_RE as _VALID_MODEL_RE  # noqa: F401
from ._claude_validation import validate_claude
from ._labels_validation import validate_labels
from ._placement_validation import validate_placement

# spec.harness (TOP-LEVEL: which agent SDK runs the session), plus its
# DEPRECATED alias spec.provider — distinct from spec.claude.provider
# (the inference backend, validated inside validate_claude via
# _provider_validation). See config._harness_types.
from ._harness_registry import valid_runtime_spellings
from ._harness_types import (
    HARNESS_KEY,
    LEGACY_HARNESS_KEY,
    harness_key_conflict_error,
    is_known_harness,
    list_harnesses,
)
from ._residency_types import residency_coupling_error, residency_value_error
from ._shape_validation import validate_autonomous, validate_proxy_coupling
from ._startup_command_validation import validate_startup_commands

# ``_VALID_MODEL_RE`` (accepted ``spec.claude.model`` shapes) moved to
# ``_claude_validation`` alongside the rest of the claude-block checks;
# re-exported at the top of this module for back-compat.

_VALID_API_VERSIONS = ("scitex-agent-container/v3",)

_KNOWN_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "metadata", "spec"})

# v3 ``kind`` discriminator. ``Agent`` = SDK runner (claude_session);
# ``AgentProxy`` = HTTP forwarder (a2a_proxy) with NO SDK. Anything
# else is rejected at parse time.
_VALID_KINDS = frozenset({"Agent", "AgentProxy"})

# spec.runtime — LAUNCH-MODE selector (operator directive 12870, lead
# a2a b58dd5d3). Repurposed from the legacy container-engine selector
# (degenerate since sac became apptainer-only on 2026-05-13). ``""``
# and ``"apptainer"`` are accepted as back-compat and mapped to
# ``"claude-agent-sdk"`` at dispatch — see ``_lifecycle/_runtime_select``.
# DERIVED from the harness registry (v4 step 4): the accepted spellings
# are whatever the descriptor entries claim, so a new harness's runtime
# spelling is one registry entry, not an edit here. The name is kept —
# it is imported elsewhere (e.g. ``_lifecycle/_relocate_probe_shell``).
_VALID_RUNTIMES = valid_runtime_spellings()

# spec.container.runtime — every value here must have a working engine
# behind it. docker/podman were ripped out 2026-05-13.
VALID_CONTAINER_RUNTIMES = frozenset({"none", "apptainer"})


_SDK_IMAGE = "scitex-agent-container:scitex"


# All spec keys read by load_v3, parsers, or a2a/_server.py.
# Unknown keys are rejected at parse time so typos surface at boot.
# Intentional extension data belongs under spec.extensions.
_KNOWN_SPEC_KEYS = frozenset(
    {
        "runtime",
        "harness",  # which agent SDK runs the session: anthropic | openai
        "provider",  # DEPRECATED alias of "harness" — still honoured so the
        # existing spec corpus loads unchanged. Unrelated to the nested
        # spec.claude.provider (inference backend). See _harness_types.
        "residency",  # v4 residency axis: resident (default) | one-shot —
        # does the daemon outlive its work? DEFAULTED, not required: a NEW
        # axis the live corpus predates; requiring it would red-start the
        # fleet for declaring nothing new. See _residency_types.
        "access",  # host-access posture: full (default) | capsule
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
        # ADR-0006/0018 — WHICH to_home cascade layers this agent inherits.
        # `_types`/`_loaders` gained the field but this allowlist did not, so
        # every spec declaring it failed to load with "Unknown spec field" —
        # which made the whole declaration mechanism unreachable, and would
        # have made the fleet-wide migration sweep write 101 specs that then
        # could not be parsed. OPTIONAL, deliberately: it is absent from the
        # explicit-required map in `_explicit_fields`, so a spec that omits it
        # still loads and still inherits the implicit cascade. Turning that
        # omission into an error is a separate, later step.
        "to_home_layers",
        "comms",  # Phase-3 ACL: outbound/inbound + a2a listen toggle
        "lineage",  # Phase-3 ACL: group=solitary + may_spawn
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


# ---------------------------------------------------------------------------
# Required author-facing fields. The 2026-06-23 "no hidden defaults" subset
# (runtime/workdir/apptainer.{image,binds}/health/restart/claude.model) was
# SUPERSEDED by the 2026-07-21 red-start ruling: EVERY spec field must be
# written explicitly, an omitted field is a load error, and the hint lists
# every missing field at once with a paste-ready block. The required-key map
# and the consolidated message live in the sibling ``_explicit_fields`` /
# ``_explicit_validation`` modules (single source of truth — ``load_v3``
# raises through the same map for direct callers); ``validate_raw`` delegates
# below so ``sac agents check`` reports identically to ``load_config``.


# ``_validate_provider`` moved to ``_provider_validation.validate_provider``
# (ADR-0011 extension — provider as registered string identifier; see
# the sibling module for the dict + string forms).


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

    # metadata.labels — abolished classification fields (``tags``, removed
    # 2026-07-19 in favour of the ``groups`` SSoT). See _labels_validation.
    errors.extend(validate_labels(metadata, path))

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

        # spec.runtime — LAUNCH-MODE selector (operator directive 12870,
        # lead a2a b58dd5d3). Repurposed from the legacy container-engine
        # selector (degenerate since sac became apptainer-only on
        # 2026-05-13). See _VALID_RUNTIMES + _runtime_select for the
        # accepted values + back-compat mapping.
        runtime = spec.get("runtime")
        if runtime and runtime not in _VALID_RUNTIMES:
            errors.append(
                f"spec.runtime must be one of {sorted(_VALID_RUNTIMES)} "
                f"(got '{runtime}'). 'tui' (default, '' maps here) = "
                "interactive in-apptainer Claude TUI; 'claude-agent-sdk' "
                "= headless SDK runner; 'apptainer' = back-compat for the "
                "pre-2026-06-13 container-engine field, mapped to "
                "'claude-agent-sdk' at dispatch."
            )

        # spec.harness — WHICH AGENT SDK runs the session. Distinct from
        # spec.claude.provider (ProviderSpec: the inference backend,
        # validated inside validate_claude). Presence is enforced by the
        # explicit-fields map (red-start ruling 2026-07-21); this check
        # owns the CONFLICT and VALUE diagnostics.
        #
        # A STATED disagreement between the two keys is its own error,
        # and its message names both values — see _harness_types.
        errors.extend(harness_key_conflict_error(spec))
        # The VALUE check runs per WRITTEN key (not on the resolved
        # value) so the error names the line the operator has to edit,
        # and so an illegal value is caught in either spelling.
        for key in (HARNESS_KEY, LEGACY_HARNESS_KEY):
            value = spec.get(key)
            if value and not is_known_harness(str(value)):
                errors.append(
                    f"spec.{key} must be one of {list_harnesses()} "
                    f"(got '{value}'). 'anthropic' (default) = the "
                    "claude-agent-sdk harness; 'openai' = the "
                    "openai-agents SDK harness; 'codex' = the "
                    "openai-codex (Codex SDK) harness. ('provider' is "
                    "the DEPRECATED alias of 'harness'; neither is "
                    "spec.claude.provider, which selects an "
                    "Anthropic-compatible inference backend — note "
                    "'codex' is a legal value of BOTH, and they mean "
                    "different things: as a HARNESS the codex agent "
                    "program runs the loop, as spec.claude.provider it "
                    "is an inference gateway with Claude Code still "
                    "driving.)"
                )

        # spec.residency — DOES THE DAEMON OUTLIVE ITS WORK (v4 step 6):
        # resident (default) parks awaiting more turns; one-shot exits
        # cleanly (ExitRecord reason oneshot-complete) when the
        # conversation completes. Absence is the resident default — the
        # live corpus predates the axis — but an ILLEGAL value and the
        # unsupported couplings (externally hosted TUI harness, kind:
        # AgentProxy) fail loud. Both checks live with the axis in
        # _residency_types.
        errors.extend(residency_value_error(spec))
        errors.extend(residency_coupling_error(spec, kind))

        # spec.access — REMOVED 2026-06-23 (SSoT: explicit binds + workdir).
        # The knob silently injected a whole-home bind, a ``/work`` alias and
        # a ``--pwd`` rewrite, so a reader of the spec could not tell what
        # actually mounted. Fail LOUD with the exact replacement rather than
        # silently honour or ignore it.
        if spec.get("access") is not None:
            workdir = spec.get("workdir") or "<your workdir>"
            home = str(Path.home())
            errors.append(
                "spec.access has been REMOVED — host access + cwd are now "
                "declared explicitly (single source of truth). Delete the "
                "`access:` line and instead:\n"
                f"  * FULL host reach → add to apptainer.binds:  - {home}:{home}:rw\n"
                f"    and set `workdir: {workdir}` (a path under it; it is the --pwd).\n"
                "  * CAPSULE (restricted) → add ONLY the binds you want "
                "(e.g. `- <writable>:/work:rw` plus data mounts) and set "
                "`workdir: /work`.\n"
                "Whatever apptainer.binds lists is exactly what mounts; "
                "workdir is only the --pwd."
            )

        # apptainer.container_workdir — REMOVED with `access`. The workdir is
        # now mounted by an EXPLICIT bind and `spec.workdir` is the --pwd, so
        # an in-container alias field no longer does anything. Loud, not inert.
        ap_block = spec.get("apptainer")
        if isinstance(ap_block, dict) and ap_block.get("container_workdir") is not None:
            errors.append(
                "apptainer.container_workdir has been REMOVED — declare the "
                "mount explicitly in apptainer.binds (e.g. "
                "`- <host-src>:/work:rw`) and set `workdir: /work` as the "
                "--pwd. The spec's binds are the single source of truth."
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

        # spec.claude block (model alias / provider / account exclusivity /
        # resume_id UUID) — validated in the sibling ``_claude_validation``
        # module (keeps this orchestrator under the per-file cap).
        errors.extend(validate_claude(spec))

        # container.runtime — apptainer is the only implemented engine
        # (docker/podman ripout 2026-05-13).
        container = spec.get("container", {}) or {}
        cr = container.get("runtime")
        if cr and cr not in VALID_CONTAINER_RUNTIMES:
            errors.append(
                "spec.container.runtime must be "
                f"{'|'.join(sorted(VALID_CONTAINER_RUNTIMES))}, got '{cr}'"
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
        # restart.prune_on_stop — inode-hygiene opt-in (bool only).
        prune_on_stop = restart.get("prune_on_stop")
        if prune_on_stop is not None and not isinstance(prune_on_stop, bool):
            errors.append(
                "spec.restart.prune_on_stop must be a boolean, got "
                f"{type(prune_on_stop).__name__}"
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

        # host / hosts — placement. Exactly one is REQUIRED and they are
        # mutually exclusive (sibling ``_placement_validation`` module).
        errors.extend(validate_placement(spec))

        # spec.autonomous (F-CS3 drive-until-done) + kind:AgentProxy
        # coupling rules — sibling ``_shape_validation`` module.
        errors.extend(validate_autonomous(spec))
        errors.extend(validate_proxy_coupling(spec, kind))

        # spec.startup_commands destructive-command guard — REJECT an
        # unguarded recursive-force ``rm`` on a variable target (the
        # 2026-07-16 P0 landmine: ``rm -rf $HOME/proj`` one bad symlink
        # from wiping ~195 repos). Sibling ``_startup_command_validation``
        # module; allows the fixed ``rm -f $VAR`` form.
        errors.extend(validate_startup_commands(spec))

        # Phase-3 capsule-isolation: type-check ``spec.comms`` +
        # ``spec.lineage`` shapes. Detailed rules live in the
        # sibling ``_acl_validation`` module (keeps this file under
        # the per-file cap). Defaults preserve current behaviour.
        errors.extend(validate_phase3_acl(spec))

        # Reject the old `scheduling:` block — replaced by host/hosts.
        if "scheduling" in spec:
            errors.append(
                "spec.scheduling block is no longer accepted. Use spec.host "
                "(singleton, optionally with fallback list) or spec.hosts "
                "(multi-instance, 'all' or list)."
            )

        # Red-start explicit-fields ruling (2026-07-21): EVERY spec field
        # must be written explicitly. host/hosts presence is enforced above
        # (mutually-exclusive pair — cannot live in a require-all map); the
        # consolidated one-error-lists-everything message with the
        # paste-ready block comes from the SSOT module.
        from ._explicit_validation import explicit_field_errors

        errors.extend(explicit_field_errors(raw, path))

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
