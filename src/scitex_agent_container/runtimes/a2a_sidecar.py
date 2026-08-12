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
  stderr to ``<runtime>/<agent>/a2a-sidecar.log``, writes the PID to
  ``<runtime>/<agent>/a2a-sidecar.pid``. No-op if ``spec.a2a.port`` is
  unset.
* :func:`stop_sidecar` — called from ``ClaudeCodeRuntime.stop`` (and
  ``cleanup``). Reads the PID file, sends SIGTERM, removes the file.
  No-op if the file is absent.

Errors are logged and swallowed — a failed sidecar must NOT block
agent start/stop. The PID file is the source of truth; if the file
is gone the sidecar is considered stopped.

**Per-agent keying.** The pid/log pair used to live at
``{workdir}/a2a-sidecar.{pid,log}``, with NO agent identity in the
path. Two agents sharing one workdir therefore shared one pid file,
and the second :func:`start_sidecar` logged "already running;
skipping" and handed back the *other* agent's PID — a silent
false-positive: the second agent looked healthy while having no
sidecar of its own and being undriveable over A2A. That is latent only
while every agent owns its workdir; cloning / twinning / relocating
breaks the assumption. The files now live in the agent's own runtime
state dir (``state_dir_for_config`` — the same
``<runtime-root>/<agent>/`` that already holds ``pid``,
``heartbeat.json``, ``session.jsonl`` and ``runner.log``), so the
identity is in the DIRECTORY, exactly like every other per-agent
artefact. A config with no ``name`` REFUSES to resolve a path rather
than falling back to a shared one.

**Migrating a sidecar started before that change.** A live pre-keying
sidecar is still recorded at ``{workdir}/a2a-sidecar.pid`` and still
holds the port; ignoring it would make the next spawn fail to bind.
Both entry points therefore consult the legacy path as well — but only
adopt/kill a process the kernel confirms is *ours*
(``/proc/<pid>/cmdline`` names THIS agent's spec file). A stranger's
pre-keying sidecar is left strictly alone.
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


def _state_dir(config: AgentConfig) -> Path:
    """Return the agent's own runtime state dir. REFUSES an unnamed agent.

    Delegates to ``tui_session.state_dir_for_config`` — the SSOT both
    host runtimes already use for per-agent files — so the sidecar's
    pid/log land next to that agent's ``pid`` / ``heartbeat.json`` /
    ``session.jsonl`` and relocate with ``$SCITEX_AGENT_CONTAINER_
    RUNTIME_DIR`` like everything else under ``runtime/<agent>/``.

    An empty ``config.name`` raises instead of degrading to a shared
    path: a silent fallback here would reproduce the very collision
    the per-agent keying exists to prevent. Both call sites already
    log-and-continue around this module, so refusing costs at most the
    sidecar, never the agent.
    """
    name = (getattr(config, "name", "") or "").strip()
    if not name:
        raise ValueError(
            "a2a sidecar: refusing to resolve a pid/log path for an agent "
            "with an empty name — an unnamed agent would silently share one "
            "sidecar with every other unnamed agent"
        )
    # Imported lazily: ``tui_session`` pulls the runner package in, and
    # ``_runners._tmux.claude_code`` imports THIS module at module scope.
    from .tui_session import state_dir_for_config

    return state_dir_for_config(config)


def _pid_path(config: AgentConfig) -> Path:
    return _state_dir(config) / PID_FILENAME


def _log_path(config: AgentConfig) -> Path:
    return _state_dir(config) / LOG_FILENAME


def _legacy_pid_path(config: AgentConfig) -> Path:
    """The PRE-KEYING, workdir-shared pid path. Read-only compatibility."""
    return Path(config.expanded_workdir) / PID_FILENAME


def _read_a2a_block(config: AgentConfig) -> dict[str, Any] | None:
    """Return ``spec.a2a`` from the agent YAML, or None if missing/disabled."""
    if not config.config_path:
        return None
    yaml_path = Path(config.config_path)
    if not yaml_path.exists():
        return None
    try:
        v3 = yaml.safe_load(yaml_path.read_text()) or {}
    except (
        OSError,
        yaml.YAMLError,
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
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
    except (
        ProcessLookupError,
        PermissionError,
    ):  # stx-allow: fallback (reason: process probe expected failure)
        return False
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return False
    return True


def _proc_argv(pid: int) -> list[str]:
    """The kernel's own record of ``pid``'s argv, or ``[]`` if unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _legacy_sidecar_pid(config: AgentConfig) -> int | None:
    """PID recorded at the legacy workdir path, iff that process is OURS.

    Ownership is settled by the kernel, not by the file: a sidecar is
    spawned as ``... a2a serve <config_path> ...``, and two agents
    sharing a workdir necessarily have DISTINCT spec files, so the
    config path in ``/proc/<pid>/cmdline`` names the owner without
    ambiguity. Returns None when the file is absent, unparseable, names
    a dead process, or names a process belonging to a different agent —
    a stranger's sidecar is never adopted and never killed.
    """
    legacy = _legacy_pid_path(config)
    if not legacy.exists():
        return None
    try:
        pid = int(legacy.read_text().strip())
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: file system operation failure)
        return None
    if pid <= 0 or not _process_alive(pid):
        return None
    if str(config.config_path) not in _proc_argv(pid):
        return None
    return pid


def _adopt_legacy_sidecar(config: AgentConfig, pid_path: Path) -> int | None:
    """Move a live pre-keying sidecar's record to the per-agent path.

    Adoption rather than a kill+respawn: the process is already serving
    this agent's port, so re-pointing the bookkeeping costs no downtime
    and no port churn. Returns the adopted PID, or None when there is
    no legacy sidecar of ours to adopt.
    """
    pid = _legacy_sidecar_pid(config)
    if pid is None:
        return None
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid))
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        log.warning("a2a sidecar: cannot write PID file %s: %s", pid_path, exc)
        return pid
    try:
        _legacy_pid_path(config).unlink()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        pass
    log.info(
        "a2a sidecar for %s adopted from legacy %s (pid=%d); record now at %s",
        config.name,
        _legacy_pid_path(config),
        pid,
        pid_path,
    )
    return pid


def start_sidecar(config: AgentConfig) -> int | None:
    """Spawn the A2A sidecar for ``config``. Return PID, or None if disabled."""
    a2a = _read_a2a_block(config)
    if a2a is None:
        return None

    pid_path = _pid_path(config)
    if pid_path.exists():
        try:
            existing = int(pid_path.read_text().strip())
        except (
            OSError,
            ValueError,
        ):  # stx-allow: fallback (reason: file system operation failure)
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
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            pass

    # First start after the per-agent keying landed: our own sidecar may
    # still be recorded at the legacy workdir path and still holding the
    # port. Adopt it instead of spawning a second one that cannot bind.
    adopted = _adopt_legacy_sidecar(config, pid_path)
    if adopted is not None:
        return adopted

    port = int(a2a["port"])
    host = str(a2a.get("host", "127.0.0.1"))
    handler = str(a2a.get("handler", "echo"))

    workdir = Path(config.expanded_workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)

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
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
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
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        log.warning("a2a sidecar: spawn failed for %s: %s", config.name, exc)
        if log_fp is not None:
            log_fp.close()
        return None
    finally:
        if log_fp is not None:
            log_fp.close()

    try:
        pid_path.write_text(str(proc.pid))
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
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


def _terminate_recorded(config: AgentConfig, pid_path: Path, pid: int) -> bool:
    """SIGTERM ``pid`` and drop ``pid_path``. Always removes the file."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (
        OSError
    ) as exc:  # stx-allow: fallback (reason: file system operation failure)
        log.warning("a2a sidecar: kill %d failed for %s: %s", pid, config.name, exc)
    log.info("a2a sidecar for %s stopped (pid=%d)", config.name, pid)
    try:
        pid_path.unlink()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        pass
    return True


def _stop_legacy_sidecar(config: AgentConfig) -> bool:
    """Tear down a live pre-keying sidecar of ours. Return True if killed.

    Without this an agent that is stopped (not restarted) after the
    keying change would leave its pre-keying sidecar running forever,
    still holding the port against the next start.
    """
    pid = _legacy_sidecar_pid(config)
    if pid is None:
        return False
    return _terminate_recorded(config, _legacy_pid_path(config), pid)


def stop_sidecar(config: AgentConfig) -> bool:
    """Stop the A2A sidecar for ``config``. Return True if a process was killed."""
    pid_path = _pid_path(config)
    if not pid_path.exists():
        return _stop_legacy_sidecar(config)
    try:
        pid = int(pid_path.read_text().strip())
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: file system operation failure)
        try:
            pid_path.unlink()
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            pass
        return _stop_legacy_sidecar(config)

    if pid <= 0 or not _process_alive(pid):
        try:
            pid_path.unlink()
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            pass
        return _stop_legacy_sidecar(config)

    return _terminate_recorded(config, pid_path, pid)
