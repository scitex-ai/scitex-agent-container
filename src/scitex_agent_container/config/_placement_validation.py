"""``spec.host`` / ``spec.hosts`` placement validation.

Extracted from ``_validation.py`` to keep that orchestrator under the
512-line cap (sibling to ``_claude_validation`` / ``_shape_validation``).

Exactly one of ``host`` (singleton) / ``hosts`` (multi-instance) is
REQUIRED — no hidden 'local' default (operator directive 2026-06-23) —
and the two are mutually exclusive.
"""

from __future__ import annotations


def validate_placement(spec: dict) -> list[str]:
    """Return placement (host/hosts) validation errors (empty = valid)."""
    errors: list[str] = []

    has_host = "host" in spec
    has_hosts = "hosts" in spec
    if not has_host and not has_hosts:
        errors.append(
            "spec.host or spec.hosts is REQUIRED — declare placement "
            "explicitly (no hidden 'local' default; operator directive "
            "2026-06-23). Use ONE of:\n"
            "  host: local              # this (the invoking) host\n"
            "  host: <peer>             # pinned to a single peer\n"
            "  hosts: [<peer>, ...]     # one instance per host (or 'all')"
        )
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

    return errors


__all__ = ["validate_placement"]
