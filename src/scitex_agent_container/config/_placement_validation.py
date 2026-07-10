"""``spec.host`` / ``spec.hosts`` placement validation.

Extracted from ``_validation.py`` to keep that orchestrator under the
512-line cap (sibling to ``_claude_validation`` / ``_shape_validation``).

Exactly one of ``host`` (singleton) / ``hosts`` (multi-instance) is
REQUIRED — no hidden 'local' default (operator directive 2026-06-23) —
and the two are mutually exclusive.

``host: local`` / ``host: localhost`` are BANNED (operator directive
2026-07-10): placement must carry the RESOLVED hostname so the spec
means the same machine no matter where it is read from — the caller's
location resolves the verb routing (local launch vs ssh dispatch to a
registered peer), not a relative spelling. Portable in-repo fixtures
that must run wherever they are started write ``host: ${HOSTNAME}``,
which the loader resolves to the concrete canonical hostname at load
time (see ``config._host`` / ``config._loaders``).
"""

from __future__ import annotations

# Relative "this machine" spellings rejected in placement fields. The
# comparison is case-insensitive on the stripped value.
_BANNED_LOCAL_SPELLINGS = frozenset({"local", "localhost"})


def _local_ban_error(field: str, value: str) -> str:
    """Actionable migration message for a banned relative host spelling."""
    return (
        f"spec.{field}: '{value}' is BANNED — placement must carry the "
        "RESOLVED hostname (operator directive 2026-07-10) so lifecycle "
        "verbs can route the spec to the right machine from anywhere. "
        "Fix ONE of:\n"
        "  host: <this-machine>   # concrete canonical hostname — run "
        "`hostname -s` (e.g. host: ywata-note-win)\n"
        "  host: ${HOSTNAME}      # portable fixtures only — resolves to "
        "the loading machine's hostname at load time\n"
        "A non-local hostname must match a peer in the host registry "
        "(~/.scitex/agent-container/config.yaml; see `sac host list`)."
    )


def _is_banned_local(value: object) -> bool:
    """True when ``value`` is a banned relative spelling ('local'/...)."""
    return isinstance(value, str) and value.strip().lower() in _BANNED_LOCAL_SPELLINGS


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
            "  host: <hostname>         # RESOLVED name — `hostname -s` here,\n"
            "                           # or a `sac host list` peer to pin remote\n"
            "  host: ${HOSTNAME}        # portable fixtures: resolves at load time\n"
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
        elif _is_banned_local(host_val):
            errors.append(_local_ban_error("host", host_val.strip()))
        elif isinstance(host_val, list):
            for h in host_val:
                if _is_banned_local(h):
                    errors.append(_local_ban_error("host", h.strip()))
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
        elif isinstance(hosts_val, list):
            for h in hosts_val:
                if _is_banned_local(h):
                    errors.append(_local_ban_error("hosts", h.strip()))
        elif not isinstance(hosts_val, (str, list)):
            errors.append(
                f"spec.hosts must be 'all' or a list of strings; "
                f"got {type(hosts_val).__name__}"
            )

    return errors


__all__ = ["validate_placement"]
