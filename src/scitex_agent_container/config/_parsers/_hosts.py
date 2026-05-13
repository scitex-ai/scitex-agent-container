"""``spec.host`` / ``spec.hosts`` + ``spec.scheduling`` parsers."""

from __future__ import annotations

from .._types import HostsSpec, SchedulingSpec


def parse_hosts_spec(spec: dict) -> "HostsSpec":
    """Parse ``spec.host`` / ``spec.hosts`` (mutually exclusive).

    Returns a ``HostsSpec``. Validation of mutual exclusion + value types
    happens in ``_validation.py``; this parser just normalizes shapes:

    * ``host: <str>``    → ``host=str, hosts=""``
    * ``host: [list]``   → ``host=list, hosts=""``
    * ``host:`` (None)   → ``host="", hosts=""``  (local singleton)
    * ``hosts: "all"``   → ``host="", hosts="all"``
    * ``hosts: [list]``  → ``host="", hosts=list``
    """
    host_raw = spec.get("host", None) if "host" in spec else None
    hosts_raw = spec.get("hosts", None) if "hosts" in spec else None

    host: str | list[str] = ""
    hosts: str | list[str] = ""

    if host_raw is not None:
        if isinstance(host_raw, list):
            host = [str(h) for h in host_raw]
        elif isinstance(host_raw, str):
            host = host_raw
        # any other type is caught by the validator; treat as empty here
    if hosts_raw is not None:
        if isinstance(hosts_raw, list):
            hosts = [str(h) for h in hosts_raw]
        elif isinstance(hosts_raw, str):
            hosts = hosts_raw

    return HostsSpec(host=host, hosts=hosts)


_VALID_SCHEDULING_MODES = ("per-host", "singleton")


def parse_scheduling(spec: dict) -> tuple[SchedulingSpec, bool]:
    """Parse ``spec.scheduling`` block (new shared-host layout).

    Returns a ``(scheduling, explicit)`` tuple. ``explicit`` is True iff
    the YAML declared a ``spec.scheduling`` key — this gates effective-id
    composition so legacy v2 YAMLs (no scheduling block, ``metadata.name``
    already baked with host) remain byte-identical to pre-change behavior.
    """
    if "scheduling" not in spec:
        return SchedulingSpec(), False
    raw = spec.get("scheduling") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"spec.scheduling must be a mapping, got {type(raw).__name__}")
    mode = raw.get("mode", "per-host") or "per-host"
    if mode not in _VALID_SCHEDULING_MODES:
        raise ValueError(
            f"spec.scheduling.mode must be one of {_VALID_SCHEDULING_MODES}, "
            f"got {mode!r}"
        )
    preferred = raw.get("preferred-host", raw.get("preferred_host", "")) or ""
    fallback_raw = raw.get("fallback-hosts", raw.get("fallback_hosts", [])) or []
    if isinstance(fallback_raw, str):
        fallback_raw = [fallback_raw]
    fallback = [str(h) for h in fallback_raw]
    return (
        SchedulingSpec(
            mode=mode,
            preferred_host=str(preferred),
            fallback_hosts=fallback,
        ),
        True,
    )
