"""``spec.autonomous`` + ``kind: AgentProxy`` coupling validation.

Extracted from ``_validation.py`` to keep that orchestrator under the
512-line cap (sibling to ``_claude_validation`` / ``_placement_validation``).
"""

from __future__ import annotations


def validate_autonomous(spec: dict) -> list[str]:
    """Return ``spec.autonomous`` (F-CS3 drive-until-done) errors."""
    errors: list[str] = []
    autonomous = spec.get("autonomous")
    if autonomous is None:
        return errors
    if not isinstance(autonomous, dict):
        errors.append(
            f"spec.autonomous must be a mapping; got {type(autonomous).__name__}"
        )
        return errors
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
    return errors


def validate_proxy_coupling(spec: dict, kind: object) -> list[str]:
    """Return ``kind``↔``spec.proxy`` coupling errors.

    AgentProxy has NO SDK — it's a thin HTTP forwarder. So:
      * spec.proxy is REQUIRED (no upstream → nothing to forward to)
      * spec.claude / spec.startup_prompts / spec.startup_commands are
        IGNORED (no SDK to configure / prompt); authoring them is a
        category error surfaced loudly.
    The mirror also holds for kind: Agent — spec.proxy is rejected there
    because the SDK runner doesn't read it.
    """
    errors: list[str] = []
    if kind == "AgentProxy":
        proxy_block = spec.get("proxy")
        if proxy_block is None:
            errors.append(
                "spec.proxy is required when kind: AgentProxy "
                "(no upstream to forward to)."
            )
        elif not (isinstance(proxy_block, dict) and proxy_block.get("upstream")):
            errors.append(
                "spec.proxy.upstream is REQUIRED when kind: AgentProxy — "
                "declare the forwarding target explicitly:\n"
                "  proxy:\n    upstream: http://127.0.0.1:9000"
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
    return errors


__all__ = ["validate_autonomous", "validate_proxy_coupling"]
