"""Native proxy startup ownership and bounded shutdown fixtures (no model calls)."""

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil
import pytest

from ._a2a_proxy_workspace import proxy_workspace


def wait_for_proxy(child, state_dir, upstream, *, timeout):
    """Wait for this PID's kernel-selected listener and actual health identity.

    A free-port probe followed by ANY /health 200 can instead observe a foreign
    server while our child is still fetching its upstream card. Port zero lets
    the native server allocate its socket atomically; no inherited-FD seam or
    production test knob is needed.
    """
    deadline = time.monotonic() + timeout
    process = psutil.Process(child.pid)
    opener = build_opener(ProxyHandler({}))
    expected = {"status": "ok", "upstream": upstream, "trust": "untrusted"}
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Owned proxy exited before readiness")
        try:
            if int((state_dir / "pid").read_text()) != child.pid:
                raise RuntimeError("Owned proxy PID record identifies another process")
            # psutil 6 renamed connections; retain its declared 5.9 compatibility.
            connections = getattr(process, "net_connections", None)
            if connections is None:
                connections = process.connections
            for connection in connections(kind="tcp"):
                if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                    continue
                if connection.laddr.ip != "127.0.0.1":
                    continue
                port = connection.laddr.port
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with opener.open(
                    f"http://127.0.0.1:{port}/health", timeout=min(0.2, remaining)
                ) as response:
                    if response.status == 200 and json.load(response) == expected:
                        return port
        except psutil.NoSuchProcess as exc:
            raise RuntimeError("Owned proxy exited before readiness") from exc
        except (OSError, URLError, ValueError):
            pass  # A child still starting may not have published PID/HTTP yet.
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    raise TimeoutError("Owned proxy did not become ready within the startup budget")


def _snapshot(child, state_dir):
    """Only owned process metadata; never environment values or ambient files."""
    result = {"pid": child.pid, "returncode": child.poll()}
    try:
        result["kernel_state"] = psutil.Process(child.pid).status()
        result["kernel_wait"] = Path(f"/proc/{child.pid}/wchan").read_text()
    except (psutil.Error, OSError) as exc:
        result["kernel_probe"] = type(exc).__name__
    for label, filename in (("kernel_syscall", "syscall"), ("kernel_stack", "stack")):
        try:
            result[label] = Path(f"/proc/{child.pid}/{filename}").read_text()[:4096]
        except OSError as exc:
            result[label] = type(exc).__name__
    try:
        result["fd_targets"] = {
            fd.name: str(fd.readlink())[:256]
            for fd in list(Path(f"/proc/{child.pid}/fd").iterdir())[:64]
        }
    except OSError as exc:
        result["fd_probe"] = type(exc).__name__
    try:
        result["heartbeat_state"] = json.loads(
            (state_dir / "heartbeat.json").read_text()
        )["state"]
    except (OSError, ValueError, KeyError) as exc:
        result["heartbeat_probe"] = type(exc).__name__
    return result


@pytest.fixture
def main_short_run(real_upstream_server):
    """Signal only a ready native child, preserving the original 5s/2s budgets."""
    base, _recorder = real_upstream_server
    with proxy_workspace() as (root, child_env):
        state_root = root / "rt"
        state_dir = state_root / "px-main"
        args = [
            sys.executable,
            "-m",
            "scitex_agent_container._runners.a2a_proxy",
            "--name",
            "px-main",
            "--upstream",
            base,
            "--redact",
            "a, ,b",
            "--state-root",
            str(state_root),
            "--tick-seconds",
            "0.05",
            "--a2a-port",
            "0",
        ]
        with tempfile.TemporaryFile(dir=root) as output:
            child = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, env=child_env
            )
            try:
                wait_for_proxy(child, state_dir, base, timeout=5.0)
                child.send_signal(signal.SIGTERM)
                rc = child.wait(timeout=2.0)
            except Exception as exc:
                snapshot = _snapshot(child, state_dir)
                output.seek(0, 2)
                output.seek(max(0, output.tell() - 8192))
                tail = output.read(8192).decode("utf-8", errors="replace")
                exc.add_note(
                    f"Owned proxy diagnostics: {snapshot}; output tail: {tail}"
                )
                raise
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2.0)
        yield {"rc": rc, "state_dir": state_dir}
