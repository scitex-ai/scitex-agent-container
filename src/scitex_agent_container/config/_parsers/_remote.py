"""Parser for ``spec.remote``."""

from __future__ import annotations

from .._types import RemoteSpec


def parse_remote(spec: dict) -> RemoteSpec:
    raw = spec.get("remote", {})
    if raw is None:
        raw = {}

    # New: list of SSH config aliases → chain format
    if isinstance(raw, list):
        hops = [str(h) for h in raw if h]
        return RemoteSpec(hops=hops)

    # New: single string → single hop (legacy single-host shorthand)
    if isinstance(raw, str):
        return RemoteSpec(hops=[raw] if raw.strip() else [], host=raw.strip())

    # Legacy: dict with explicit host/user/key fields
    return RemoteSpec(
        host=raw.get("host", ""),
        user=raw.get("user", ""),
        key=raw.get("key", ""),
        port=int(raw.get("port", 22)),
        login_shell=raw.get("login_shell", True),
        no_preflight=raw.get("no_preflight", False),
    )
