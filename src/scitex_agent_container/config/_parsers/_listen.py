"""Parser for ``spec.listen`` port/socket declarations."""

from __future__ import annotations

from .._types import ListenPort


def parse_listen(spec: dict) -> list[ListenPort]:
    """Parse ``spec.listen`` port/socket declarations.

    Container does NOT bind these — declarations only. Entries that
    fail validation (missing port for tcp/udp, missing path for unix)
    are silently dropped so a malformed side-entry can't break startup.
    """
    raw = spec.get("listen", []) or []
    out: list[ListenPort] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        proto = str(item.get("proto", "tcp") or "tcp")
        try:
            port = int(item.get("port", 0) or 0)
        except (
            TypeError,
            ValueError,
        ):  # stx-allow: fallback (reason: type coercion or format mismatch)
            port = 0
        path = str(item.get("path", "") or "")
        if proto in ("tcp", "udp") and port <= 0:
            continue
        if proto == "unix" and not path:
            continue
        out.append(
            ListenPort(
                port=port,
                proto=proto,
                path=path,
                name=str(item.get("name", "") or ""),
                owner=str(item.get("owner", "") or ""),
            )
        )
    return out
