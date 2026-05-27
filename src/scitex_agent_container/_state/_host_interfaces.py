"""Local network-interface inventory for ``sac host list`` (extracted).

Lifted out of :mod:`scitex_agent_container._state.host_config` to keep
the config schema file under the per-file line cap. No behaviour change
— this is a pure extraction. The public import path
``from scitex_agent_container._state.host_config import host_interfaces``
is preserved by a one-line re-export in ``host_config.py``.

Sits next to :mod:`._host_ssh` (the matching ssh-argv extraction).
"""

from __future__ import annotations


def host_interfaces() -> list[dict]:
    """Best-effort inventory of local network interfaces.

    Surfaced by ``sac host list`` and (eventually) recorded in
    ``state.db.host_interfaces``. Tailscale / wireguard / ssh-tunnel
    detection is heuristic — parses ``ip -j addr`` when available,
    falls back to a single ``hostname -I`` summary on failure.
    """
    import json
    import subprocess

    rows: list[dict] = []
    # stx-allow: fallback (reason: ip(8) missing on macOS / minimal
    # containers; we degrade to a single summary row)
    try:
        out = subprocess.run(
            ["ip", "-j", "addr"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        for iface in json.loads(out or "[]"):
            name = iface.get("ifname")
            for ai in iface.get("addr_info", []) or []:
                addr = ai.get("local")
                family = ai.get("family")
                if addr and family in ("inet", "inet6"):
                    rows.append({"iface": name, "addr": addr, "family": family})
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
    ):  # stx-allow: fallback (reason: see inline comment)
        pass

    if not rows:
        # stx-allow: fallback (reason: hostname -I is universal but
        # collapses every interface; better than nothing)
        try:
            out = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            for addr in (out or "").split():
                rows.append({"iface": "?", "addr": addr, "family": "inet"})
        except (
            FileNotFoundError,
            subprocess.SubprocessError,
        ):  # stx-allow: fallback (reason: see inline comment)
            pass

    return rows


__all__ = ["host_interfaces"]
