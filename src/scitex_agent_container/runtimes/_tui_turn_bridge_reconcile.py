"""Reload only a live TUI agent's turn bridge from authoritative state."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .._runners._tmux._target import exact_target
from ..config import AgentConfig
from ._tui_turn_bridge_lifecycle import resolved_a2a_host, start_turn_bridge
from .tui_session import session_name_for


@dataclass(frozen=True)
class TurnBridgeReconcileResult:
    """Observed and, when requested, applied bridge-only reconciliation."""

    name: str
    status: str
    port: int
    source: str
    session: str
    prior_bridge_pid: int | None
    bridge_pid: int | None
    tui_preserved: bool
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _session_alive(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", exact_target(session)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _bridge_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _read_bridge_pid(config: AgentConfig) -> int | None:
    from ._tui_turn_bridge_lifecycle import _pid_path

    try:
        value = int(_pid_path(config).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _live_port(config: AgentConfig, *, current_host: str) -> tuple[int, str]:
    """Resolve auto from the live instance/claim registry; never allocate."""
    from ..cli_pkg._send_resolve import resolve_send_endpoint

    endpoint = resolve_send_endpoint(config.name, current_host=current_host)
    return _select_live_port(config, endpoint=endpoint, current_host=current_host)


def _select_live_port(
    config: AgentConfig, *, endpoint: Any, current_host: str
) -> tuple[int, str]:
    """Fold a declared port and one observed registry endpoint."""
    a2a = getattr(config, "a2a", None)
    declared = getattr(a2a, "port", None) if a2a is not None else None
    observed = endpoint.a2a_port
    if isinstance(declared, str) and declared == "auto":
        if observed is None:
            raise RuntimeError(
                f"agent {config.name!r} declares a2a.port=auto but the live "
                "instance/port registry has no bound port; reload refuses to "
                "allocate a different endpoint for an existing TUI"
            )
        if endpoint.host != current_host:
            raise RuntimeError(
                f"agent {config.name!r} is registered on {endpoint.host!r}, not "
                f"this host {current_host!r}; run the reconcile there"
            )
        return observed, endpoint.source
    if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
        raise RuntimeError(f"agent {config.name!r} has no enabled a2a.port")
    if observed is not None and observed != declared:
        raise RuntimeError(
            f"agent {config.name!r} declares pinned a2a.port={declared}, but "
            f"authoritative live state records {observed}; refusing to guess"
        )
    return declared, "spec" if observed is None else endpoint.source


def reconcile_turn_bridge(
    config: AgentConfig,
    *,
    apply: bool,
    current_host: str,
    live_port_fn: Callable[..., tuple[int, str]] = _live_port,
    session_alive_fn: Callable[[str], bool] = _session_alive,
    bridge_pid_read_fn: Callable[[AgentConfig], int | None] = _read_bridge_pid,
    pid_alive_fn: Callable[[int | None], bool] = _pid_alive,
    bridge_start: Callable[[AgentConfig], int | None] = start_turn_bridge,
    bridge_ready_fn: Callable[[str, int], bool] = _bridge_ready,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    ready_timeout_s: float = 5.0,
) -> TurnBridgeReconcileResult:
    """Reload the sidecar only, proving the live TUI survives unchanged."""
    if config.runtime != "tui":
        raise RuntimeError(
            f"agent {config.name!r} uses runtime={config.runtime!r}; this command "
            "only reconciles a live TUI turn bridge"
        )
    session = session_name_for(config)
    if not session_alive_fn(session):
        raise RuntimeError(
            f"tmux session {session!r} is not live; bridge-only reconcile will "
            "not start or restart the agent"
        )
    port, source = live_port_fn(config, current_host=current_host)
    prior_pid = bridge_pid_read_fn(config)
    prior_alive = pid_alive_fn(prior_pid)
    config.a2a.port = port
    if not apply:
        return TurnBridgeReconcileResult(
            name=config.name,
            status="would-reload" if prior_alive else "would-start",
            port=port,
            source=source,
            session=session,
            prior_bridge_pid=prior_pid,
            bridge_pid=prior_pid,
            tui_preserved=True,
            applied=False,
        )
    bridge_pid = bridge_start(config)
    if not isinstance(bridge_pid, int) or bridge_pid <= 0:
        raise RuntimeError(
            f"turn bridge for {config.name!r} did not return a child PID; the TUI "
            f"was preserved, but {current_host}:{port} was not reconciled"
        )
    declared_host = resolved_a2a_host(config)
    probe_host = "127.0.0.1" if declared_host in {"0.0.0.0", "::"} else declared_host
    deadline = now_fn() + ready_timeout_s
    while not bridge_ready_fn(probe_host, port):
        if now_fn() >= deadline:
            raise RuntimeError(
                f"turn bridge child pid={bridge_pid} did not listen on "
                f"{probe_host}:{port} within {ready_timeout_s:.1f}s; the TUI was "
                "preserved"
            )
        sleep_fn(0.05)
    if not session_alive_fn(session):
        raise RuntimeError(
            f"tmux session {session!r} disappeared during bridge reconciliation"
        )
    return TurnBridgeReconcileResult(
        name=config.name,
        status="reloaded" if prior_alive else "started",
        port=port,
        source=source,
        session=session,
        prior_bridge_pid=prior_pid,
        bridge_pid=bridge_pid,
        tui_preserved=True,
        applied=True,
    )


__all__ = ["TurnBridgeReconcileResult", "reconcile_turn_bridge"]
