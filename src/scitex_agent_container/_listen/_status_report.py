"""``sac listen status`` report — probe + render (card
``sac-listen-restart-selfheal-cli``).

One-command diagnosis of the listen daemon: running/down, bound
address, pidfile + its PID liveness, and a live health probe. Kept out
of ``cli_pkg/listen_cmds`` so the click verb stays a thin wrapper and
this logic is unit-testable without a CliRunner.

``http_get`` / ``port_is_bound`` are passed in by the caller (the CLI
resolves them from ``_restart`` / ``_port_holder``) so tests drive the
probe with recorded fakes — no real socket, no MagicMock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

__all__ = [
    "build_status_payload",
    "render_status_lines",
]


def build_status_payload(
    *,
    host: str,
    port: int,
    pid_file: Path,
    pidfile_pid: int | None,
    pidfile_pid_alive: bool,
    health_path: str,
    http_get: Callable[[str, float], int],
    port_is_bound: Callable[[str, int], bool],
) -> dict:
    """Probe the daemon and return a machine-readable status dict.

    ``running`` uses auth-change-proof liveness (mirrors
    ``_restart.wait_for_health``): ANY HTTP response (incl. a 401 from
    the bearer gate) proves it is serving; only ``-1`` (transport
    failure) is "down". ``port_bound`` separates "wedged" (bound but
    not answering) from "fully down" (not even bound).
    """
    port_bound = port_is_bound(host, port)
    health_url = f"http://{host}:{port}{health_path}"
    health_status = http_get(health_url, 2.0)
    serving = health_status > 0
    return {
        "bind": f"{host}:{port}",
        "running": serving,
        "port_bound": port_bound,
        "pidfile": str(pid_file),
        "pidfile_pid": pidfile_pid,
        "pidfile_pid_alive": pidfile_pid_alive,
        "health_url": health_url,
        "health_status": health_status,
    }


def render_status_lines(payload: dict) -> list[str]:
    """Render the human-readable report from a status payload.

    Returns a list of lines (the CLI echoes them). The headline names
    the three distinct states explicitly — UP / WEDGED / DOWN — so the
    operator can tell "bound but not serving" (needs ``restart
    --force``) from "fully down" (needs ``restart``) at a glance.
    """
    serving = bool(payload["running"])
    port_bound = bool(payload["port_bound"])
    health_status = payload["health_status"]
    pidfile_pid = payload["pidfile_pid"]

    if serving:
        state = "UP (serving)"
    elif port_bound:
        state = "WEDGED (port bound but not serving)"
    else:
        state = "DOWN"

    lines = [
        f"sac listen: {state}",
        f"  bind:           {payload['bind']}",
        f"  port bound:     {'yes' if port_bound else 'no'}",
        f"  pidfile:        {payload['pidfile']}",
    ]
    if pidfile_pid is None:
        lines.append("  pidfile PID:    <none/stale-empty>")
    else:
        liveness = (
            "alive" if payload["pidfile_pid_alive"] else "DEAD (stale pidfile)"
        )
        lines.append(f"  pidfile PID:    {pidfile_pid} ({liveness})")
    probe = f"HTTP {health_status}" if health_status > 0 else "no response (down)"
    lines.append(f"  health probe:   {payload['health_url']} -> {probe}")
    if not serving:
        lines.append(
            "  hint: run `sac listen restart` (add --force if a wedged "
            "remnant holds the port)."
        )
    return lines
