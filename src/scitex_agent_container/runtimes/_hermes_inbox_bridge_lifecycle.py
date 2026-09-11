"""Host-side lifecycle for Hermes' authenticated SAC inbox consumer."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .._listen._config import listen_base_url
from ..config import AgentConfig
from ._apptainer_build import _read_listen_bearer
from ._tui_turn_bridge_lifecycle import resolved_a2a_port
from .tui_session import state_dir_for_config

log = logging.getLogger(__name__)
MODULE_PATH = "scitex_agent_container.runtimes._hermes_inbox_bridge"
PID_FILENAME = "hermes-inbox-bridge.pid"
LOG_FILENAME = "hermes-inbox-bridge.log"
_STOP_GRACE_S = 5.0


def _pid_path(config: AgentConfig) -> Path:
    return state_dir_for_config(config) / PID_FILENAME


def _argv_value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _owns_bridge_process(
    pid: int,
    *,
    name: str,
    config_path: str,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Prove a PID is this bridge for this agent and exact authored spec."""
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [part.decode(errors="replace") for part in raw if part]
    return (
        MODULE_PATH in argv
        and _argv_value(argv, "--name") == name
        and _argv_value(argv, "--config-path") == config_path
    )


def bridge_running(config: AgentConfig) -> bool:
    try:
        pid = int(_pid_path(config).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _owns_bridge_process(
        pid,
        name=config.name,
        config_path=str(getattr(config, "config_path", "") or ""),
    )


def stop_inbox_bridge(
    config: AgentConfig,
    *,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Stop only the identity-proven bridge recorded for this agent."""
    path = _pid_path(config)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return False
    config_path = str(getattr(config, "config_path", "") or "")
    if not _owns_bridge_process(pid, name=config.name, config_path=config_path):
        log.warning(
            "Hermes inbox bridge PID %s is not owned by %s; not signalling",
            pid,
            config.name,
        )
        path.unlink(missing_ok=True)
        return False
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _owns_bridge_process(pid, name=config.name, config_path=config_path):
            path.unlink(missing_ok=True)
            return True
        sleep(0.05)
    if _owns_bridge_process(pid, name=config.name, config_path=config_path):
        kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return True


def _listener_accepts_bearer(url: str, bearer: str) -> None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/event-stream", "Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"SAC inbox stream returned HTTP {response.status}")


def start_inbox_bridge(
    config: AgentConfig,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    preflight: Callable[[str, str], None] = _listener_accepts_bearer,
) -> int:
    """Start one explicit-ack consumer after its listener route is reachable."""
    port = resolved_a2a_port(config)
    config_path = str(getattr(config, "config_path", "") or "")
    if port is None:
        raise RuntimeError("Hermes inbox delivery requires a resolved spec.a2a.port")
    if not config_path:
        raise RuntimeError("Hermes inbox delivery requires config.config_path")
    bearer = _read_listen_bearer()
    if not bearer:
        raise RuntimeError("SAC listen bearer is absent; refusing a deaf Hermes launch")
    stop_inbox_bridge(config)
    base_url = listen_base_url().rstrip("/")
    stream_url = f"{base_url}/agents/{config.name}/inbox/stream?ack=explicit"
    preflight(stream_url, bearer)
    state_dir = state_dir_for_config(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        MODULE_PATH,
        "--name",
        config.name,
        "--listen-url",
        base_url,
        "--turn-url",
        f"http://127.0.0.1:{port}/v1/turn",
        "--config-path",
        config_path,
    ]
    env = os.environ.copy()
    env["SAC_LISTEN_BEARER"] = bearer
    with open(state_dir / LOG_FILENAME, "ab") as output:
        process = spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        raise RuntimeError("Hermes inbox bridge spawn returned no PID")
    pid_path = _pid_path(config)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    time.sleep(0.2)
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        pid_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Hermes inbox bridge exited during startup; inspect "
            f"{state_dir / LOG_FILENAME}"
        )
    return pid


__all__ = [
    "LOG_FILENAME",
    "MODULE_PATH",
    "PID_FILENAME",
    "bridge_running",
    "start_inbox_bridge",
    "stop_inbox_bridge",
]
