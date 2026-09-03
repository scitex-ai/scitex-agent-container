"""The spec-key INVENTORY — what a v3 spec may say, and what it may not.

Extracted from ``_validation.py`` under the project's 512-line per-file
cap (same split as the sibling ``_claude_validation`` /
``_provider_validation`` / ``_shape_validation`` modules). Pure data:
three tables and no logic, so ``validate_raw`` stays the orchestrator
and the inventory can be read — and added to — without scrolling past
the walker.

``_validation`` re-exports all three names, so every existing
``from ...config._validation import _KNOWN_SPEC_KEYS`` keeps resolving.
"""

from __future__ import annotations

__all__ = [
    "_KNOWN_SPEC_KEYS",
    "_V3_RELOCATED_FIELDS",
    "_V3_REMOVED_FIELDS",
]



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
        "engines",  # MULTI-backend surface: several named engines, one
        # picked at start (``--engine <key>``). OPTIONAL, deliberately —
        # it is ABSENT from the explicit-required map in
        # ``_explicit_fields``, because ~123 deployed specs predate the
        # axis and requiring it would red-start every one of them for
        # declaring nothing new (the same posture ``residency`` and
        # ``to_home_layers`` took). See ``config._engine_types``.
        "health",
        "watchdog",
        "restart",
        "hooks",
        "startup_commands",
        "startup_prompts",  # v3-realign: separate from startup_commands (§3)
        "startup",
        "context_management",  # TOLERATED FOSSIL (2026-08-15): schema deleted — nothing ever read it; key parses to nothing. Drop from this list only after the fleet sweep strips deployed specs, else every spec red-starts (the container.runtime trap).
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
