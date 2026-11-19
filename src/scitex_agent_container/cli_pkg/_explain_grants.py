"""The ``Grants`` section of ``sac agents explain`` (and the lint wording).

Every grant is listed with WHO decided it: the spec's explicit
``spec.grants`` block, or the legacy implicit default that a later release
flips to deny. Host exec is never one line among many — it gets its own
banner, because it is the grant that turns an agent into the operator.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GRANT_SUMMARY", "grants_lines"]

_LEGACY = "implicit (legacy default; will be denied when the default flips)"

GRANT_SUMMARY: dict[str, str] = {
    "host_exec": "run arbitrary host argv via POST /v1/host_exec",
    "sac_control": "sac-builtin MCP, server:sac channel, listen URL/bearer/token",
    "host_network": "share the host network namespace",
    "host_binds": "fleet binds (cards, cct_store, emacs_handoff, host_tmp, quota_cache)",
    "fleet_env": "fleet default env, PG/board identity, runtime dir, spec dirs",
    "host_claude": "host ~/.claude commands/skills, deep-merge, shared baseline",
    "secrets": "envrc fold, GitHub token, CCT bot token, operator pool",
    "host_model_auth": "host ~/.claude OAuth dir and host-env model API keys",
}


def grants_lines(config: Any) -> list[str]:
    """The explain section, host-exec banner first."""
    from ..config._grants import GRANT_KEYS, LIST_GRANTS, host_exec_state, is_explicit

    explicit = is_explicit(config)
    held, why = host_exec_state(config)
    mark = "!!!" if held else "   "
    lines = [
        "",
        f"{mark} HOST EXEC: {'HELD' if held else 'not held'} — {why}",
        "",
        "Grants (spec.grants — explicit, deny-by-default):"
        if explicit
        else "Grants (no spec.grants block — legacy implicit authorities):",
    ]
    for key in GRANT_KEYS:
        if key == "host_exec":
            continue
        if not explicit:
            state = _LEGACY
        elif key in LIST_GRANTS:
            parts = [p for p in LIST_GRANTS[key] if config.grants.allows(key, p)]
            state = f"explicit: {parts}" if parts else "withheld"
        else:
            state = "explicit: granted" if config.grants.allows(key) else "withheld"
        lines.append(f"  {key}: {state}   [{GRANT_SUMMARY[key]}]")
    if not explicit:
        lines.append(
            f"  migrate: sac agents grants infer {getattr(config, 'name', '<name>')}"
        )
    from ..config._grants import network_refusal

    refusal = network_refusal(config)
    if refusal:
        lines.append(f"  ⚠ START WILL REFUSE: {refusal}")
    return lines
