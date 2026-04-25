"""Auto-launch / cleanup of the A2A sidecar process for a sac agent.

When an agent YAML declares ``spec.a2a.port`` (and optionally
``spec.a2a.handler`` / ``spec.a2a.host``), sac launches a foreground-
mode A2A HTTP server as a subprocess alongside the multiplexer
session. This realises the user's stated value prop: agent containers
expose an A2A endpoint at startup without requiring an explicit
``sac a2a serve`` invocation.

Lifecycle:

* :func:`start_sidecar` — called from ``ClaudeCodeRuntime.start`` after
  the multiplexer is up. ``Popen``-spawns the server, captures stdout/
  stderr to ``{workdir}/a2a-sidecar.log``, writes the PID to
  ``{workdir}/a2a-sidecar.pid``. No-op if ``spec.a2a.port`` is unset.
* :func:`stop_sidecar` — called from ``ClaudeCodeRuntime.stop`` (and
  ``cleanup``). Reads the PID file, sends SIGTERM, removes the file.
  No-op if the file is absent.

Errors are logged and swallowed — a failed sidecar must NOT block
agent start/stop. The PID file is the source of truth; if the file
is gone the sidecar is considered stopped.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from scitex_agent_container.config import AgentConfig

log = logging.getLogger(__name__)

PID_FILENAME = "a2a-sidecar.pid"
LOG_FILENAME = "a2a-sidecar.log"


def _pid_path(config: AgentConfig) -> Path:
    return Path(config.expanded_workdir) / PID_FILENAME


def _log_path(config: AgentConfig) -> Path:
    return Path(config.expanded_workdir) / LOG_FILENAME


def _read_a2a_block(config: AgentConfig) -> dict[str, Any] | None:
    """Return ``spec.a2a`` from the agent YAML, or None if missing/disabled."""
    if not config.config_path:
        return None
    yaml_path = Path(config.config_path)
    if not yaml_path.exists():
        return None
    try:
        v3 = yaml.safe_load(yaml_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("a2a sidecar: cannot parse %s: %s", yaml_path, exc)
        return None
    spec = v3.get("spec") or {}
    a2a = spec.get("a2a")
    if not isinstance(a2a, dict):
        return None
    if a2a.get("port") is None:
        return None
    return a2a


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def start_sidecar(config: AgentConfig) -> int | None:
    """Spawn the A2A sidecar for ``config``. Return PID, or None if disabled."""
    a2a = _read_a2a_block(config)
    if a2a is None:
        return None

    pid_path = _pid_path(config)
    if pid_path.exists():
        try:
            existing = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            existing = -1
        if existing > 0 and _process_alive(existing):
            log.info(
                "a2a sidecar for %s already running (pid=%d); skipping",
                config.name,
                existing,
            )
            return existing
        try:
            pid_path.unlink()
        except OSError:
            pass

    port = int(a2a["port"])
    host = str(a2a.get("host", "127.0.0.1"))
    handler = str(a2a.get("handler", "echo"))

    workdir = Path(config.expanded_workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(config)

    cmd = [
        sys.executable,
        "-m",
        "scitex_agent_container",
        "a2a",
        "serve",
        config.config_path,
        "--host",
        host,
        "--port",
        str(port),
        "--handler",
        handler,
        "-v",
    ]

    log_fp = None
    try:
        log_fp = log_path.open("ab")
    except OSError as exc:
        log.warning("a2a sidecar: cannot open log %s: %s", log_path, exc)

    stdout_target = log_fp if log_fp is not None else subprocess.DEVNULL

    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("a2a sidecar: spawn failed for %s: %s", config.name, exc)
        if log_fp is not None:
            log_fp.close()
        return None
    finally:
        if log_fp is not None:
            log_fp.close()

    try:
        pid_path.write_text(str(proc.pid))
    except OSError as exc:
        log.warning("a2a sidecar: cannot write PID file %s: %s", pid_path, exc)

    log.info(
        "a2a sidecar for %s started: pid=%d %s:%d handler=%s log=%s",
        config.name,
        proc.pid,
        host,
        port,
        handler,
        log_path,
    )
    return proc.pid


def stop_sidecar(config: AgentConfig) -> bool:
    """Stop the A2A sidecar for ``config``. Return True if a process was killed."""
    pid_path = _pid_path(config)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        try:
            pid_path.unlink()
        except OSError:
            pass
        return False

    if pid <= 0 or not _process_alive(pid):
        try:
            pid_path.unlink()
        except OSError:
            pass
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning("a2a sidecar: kill %d failed for %s: %s", pid, config.name, exc)
    log.info("a2a sidecar for %s stopped (pid=%d)", config.name, pid)
    try:
        pid_path.unlink()
    except OSError:
        pass
    return True
