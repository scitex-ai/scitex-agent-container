"""Owned sidecar fixtures: native CLI, kernel socket ownership and HTTP readiness."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest
import yaml

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.a2a_sidecar import (
    _pid_path,
    _proc_argv,
    _process_alive,
    start_sidecar,
)


def _free_port() -> int:
    """Claim and immediately release a localhost port via a real socket bind."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_pid_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_pid_alive(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _marked_sleeper(marker: Path) -> subprocess.Popen:
    """A live process whose KERNEL argv carries ``marker``.

    ``python -c ... <marker>`` rather than ``sh -c``: a shell tail-execs
    its single command and the extra argv word is lost with it, which
    would silently defeat the ownership probe this stands in for.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(marker)]
    )


def _kill(pid: int | None) -> None:
    if pid is not None and _process_alive(pid):
        os.kill(pid, 15)  # SIGTERM
        _wait_pid_dead(pid, timeout=5.0)


def _write_yaml(
    path: Path, *, port: int | None, handler: str = "echo", name: str = "sidecar-test"
) -> Path:
    """Write a minimal v3 agent YAML with the requested ``spec.a2a`` block."""
    doc: dict = {
        "apiVersion": "scitex/v3",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {},
    }
    if port is not None:
        doc["spec"]["a2a"] = {
            "port": port,
            "host": "127.0.0.1",
            "handler": handler,
        }
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


def _config(
    workdir: Path,
    *,
    config_path: str = "",
    name: str = "alpha",
) -> AgentConfig:
    return AgentConfig(
        name=name,
        runtime="apptainer",
        workdir=str(workdir),
        config_path=config_path,
    )


def wait_for_sidecar(pid, port, name, *, timeout):
    """Do not yield a process that merely exists but has not started its server.

    Preserve the fixture's existing budget. Metadata from an unrelated server
    cannot qualify a child: the PID must own the listening socket itself.
    """
    if pid is None:
        raise RuntimeError("Owned sidecar was not spawned")
    process = psutil.Process(pid)
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                raise RuntimeError("Owned sidecar exited before HTTP readiness")
            argv = _proc_argv(pid)
            owns_port = any(
                connection.laddr
                and connection.laddr.port == port
                and connection.status == psutil.CONN_LISTEN
                for connection in process.net_connections(kind="tcp")
            )
            remaining = deadline - time.monotonic()
            if str(port) in argv and owns_port and remaining > 0:
                with opener.open(
                    f"http://127.0.0.1:{port}/agents/", timeout=min(0.25, remaining)
                ) as response:
                    payload = json.loads(response.read(65537))
                if [agent["name"] for agent in payload.get("agents", [])] == [name]:
                    return
        except psutil.NoSuchProcess as exc:
            raise RuntimeError("Owned sidecar exited before HTTP readiness") from exc
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            pass  # A child still starting may not accept HTTP yet.
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise TimeoutError(
        f"Owned sidecar {name} (pid={pid}, port={port}) did not become ready"
    )


@pytest.fixture
def shared_workdir_pair(tmp_path: Path):
    """Two REAL sidecars for two agents sharing ONE workdir.

    Function-scoped (each test gets its own pair) so no assertion can
    depend on another having run first.
    """
    root = tmp_path / "pair"
    root.mkdir()
    wd = root / "SHARED-WORKDIR"
    wd.mkdir()
    port_a, port_b = _free_port(), _free_port()
    spec_a = _write_yaml(root / "a.yaml", port=port_a, name="pair-alpha")
    spec_b = _write_yaml(root / "b.yaml", port=port_b, name="pair-bravo")
    cfg_a = _config(wd, config_path=str(spec_a), name="pair-alpha")
    cfg_b = _config(wd, config_path=str(spec_b), name="pair-bravo")

    pid_a = start_sidecar(cfg_a)
    pid_b = start_sidecar(cfg_b)
    try:
        wait_for_sidecar(pid_a, port_a, cfg_a.name, timeout=10.0)
        wait_for_sidecar(pid_b, port_b, cfg_b.name, timeout=10.0)
        yield {
            "cfg_a": cfg_a,
            "cfg_b": cfg_b,
            "pid_a": pid_a,
            "pid_b": pid_b,
            "port_b": port_b,
        }
    finally:
        _kill(pid_a)
        _kill(pid_b)
        _pid_path(cfg_a).unlink(missing_ok=True)
        _pid_path(cfg_b).unlink(missing_ok=True)
