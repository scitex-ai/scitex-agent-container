"""``claude-session`` runtime adapter (Phase 1).

Spawns the Python runner under
:mod:`scitex_agent_container._runners.claude_session` as a detached
subprocess and exposes the standard :class:`RuntimeBase` lifecycle
surface (start / stop / is_running / logs).

Phase 1 scope: lifecycle only. The runner just heartbeats; no SDK
calls yet. Phase 2 wires the multi-turn SDK loop into the runner
without changing this adapter's interface.

Where state lives: see :mod:`._runners.claude_session` for the
per-agent directory layout (``pid``, ``heartbeat.json``,
``session.jsonl`` Phase 2).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from .._runners import claude_session as _runner
from ..config import AgentConfig
from .base import RuntimeBase

__all__ = ["ClaudeSessionRuntime"]


class ClaudeSessionRuntime(RuntimeBase):
    """Daemon-mode runtime backed by ``claude-agent-sdk`` (Phase 1: heartbeat only)."""

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> bool:
        """Spawn the runner detached. Returns True if PID lands within ~3 s."""
        _ = no_preflight  # Phase 1: no preflight checks beyond is_running.
        state_dir = _runner.state_dir_for(config.name)

        if force and self.is_running(config):
            self.stop(config)
            time.sleep(0.5)
        elif self.is_running(config):
            return False  # already running; caller can choose to --force

        if dry_run:
            state_dir.mkdir(parents=True, exist_ok=True)
            return True

        # Detach via ``setsid`` so the runner survives the parent. Mirror
        # the pattern used by ``runtimes.auto.daemon.run_daemon`` (no
        # double-fork required: the child is a fresh process group leader).
        argv = [
            sys.executable,
            "-m",
            "scitex_agent_container._runners.claude_session",
            "--name",
            config.name,
        ]
        # Mission: first non-empty ``spec.startup_commands[*].command`` —
        # mirrors how contributor-spec.yaml.j2 lays out the agent's task.
        mission = _first_mission(config)
        if mission:
            argv.extend(["--mission", mission])
        # Auto-resume: if a previous run persisted a session id, hand it to
        # the SDK so the new turn picks up where the old one left off.
        prior_sid = _runner.read_session_id(state_dir)
        if prior_sid:
            argv.extend(["--resume-session-id", prior_sid])
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

        # Wait briefly for the runner to write its PID file. The PID we
        # store on disk is the runner's own getpid(), which equals the
        # subprocess PID when start_new_session=True (the child is the
        # session leader). Poll instead of sleep-and-hope so a fast
        # runner doesn't pay an unnecessary delay.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if _runner.read_pid(state_dir) == proc.pid:
                return True
            if proc.poll() is not None:
                return False  # child died before writing PID
            time.sleep(0.05)
        return False

    def stop(self, config: AgentConfig) -> bool:
        """SIGTERM the runner; fall back to SIGKILL after 5 s."""
        state_dir = _runner.state_dir_for(config.name)
        pid = _runner.read_pid(state_dir)
        if pid is None:
            return True  # nothing to stop

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._cleanup_state(state_dir)
            return True
        except PermissionError:
            return False

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _pid_alive(pid):
                self._cleanup_state(state_dir)
                return True
            time.sleep(0.1)

        # Stuck — escalate.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.2)
        if not _pid_alive(pid):
            self._cleanup_state(state_dir)
            return True
        return False

    def is_running(self, config: AgentConfig) -> bool:
        """True if the recorded PID exists and the process is alive."""
        state_dir = _runner.state_dir_for(config.name)
        pid = _runner.read_pid(state_dir)
        return pid is not None and _pid_alive(pid)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Phase 1: only the latest heartbeat is available — return that.

        Phase 2 will tail ``session.jsonl`` (the assistant message
        stream) here.
        """
        _ = lines
        state_dir = _runner.state_dir_for(config.name)
        hb = _runner.read_heartbeat(state_dir)
        if hb is None:
            return "(no heartbeat yet)"
        import json as _json

        return _json.dumps(hb, indent=2)

    @staticmethod
    def _cleanup_state(state_dir) -> None:
        """Best-effort removal of pid + heartbeat files."""
        for name in ("pid", "heartbeat.json", "pid.tmp", "heartbeat.json.tmp"):
            try:
                (state_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError:  # stx-allow: fallback (reason: cleanup must not raise)
                pass


def _first_mission(config: AgentConfig) -> str | None:
    """First non-empty ``spec.startup_commands[*].command``, or None.

    Mirrors how the contributor-spec template lays out an agent's task:
    a single ``startup_commands:`` list whose first entry's ``command``
    is the agent's mission prompt. ``delay`` is ignored — the SDK
    runtime needs a one-shot prompt to seed the conversation, not a
    timed sequence of typed-in commands like the CLI runtime expects.
    """
    for entry in getattr(config, "startup_commands", []) or []:
        cmd = (getattr(entry, "command", "") or "").strip()
        if cmd:
            return cmd
    return None


def _pid_alive(pid: int) -> bool:
    """True iff PID is alive and not a zombie.

    ``kill -0`` returns success against zombies (a process that has
    exited but whose parent has not yet ``wait()``ed for it). The sac
    runner is its own session leader; if its caller forgets to reap
    it (e.g. a CLI that returns straight after ``start``), the PID
    file points at a zombie. We don't want ``is_running`` /
    ``sac show-status`` to claim such processes are alive — explicitly
    detect and exclude them.

    Linux: parse ``/proc/<pid>/status`` and look for the ``State:`` line.
    Other platforms: fall through to the ``kill -0`` answer (acceptable
    — sac targets Linux fleets; macOS / WSL behaviour matches Linux).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not ours

    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("State:"):
                    # Format: ``State:\tZ (zombie)`` etc.
                    flag = line.split()[1] if len(line.split()) > 1 else ""
                    return flag != "Z"
    except FileNotFoundError:
        # Process raced away between kill -0 and proc read; treat as gone.
        return False
    except OSError:  # stx-allow: fallback (reason: /proc may be absent on non-Linux)
        pass
    return True
