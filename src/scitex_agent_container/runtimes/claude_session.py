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
from pathlib import Path

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
        foreground: bool = False,
    ) -> bool:
        """Spawn the runner. In daemon mode, returns True once the PID
        file lands. In foreground mode, blocks until the conversation
        completes (or the operator hits Ctrl+C) and returns True iff
        the runner exited cleanly."""
        _ = no_preflight  # no preflight checks beyond is_running.
        # If the YAML lives under a project-local
        # ``.scitex/agent-container/agents/`` tree, route runtime state
        # to the matching ``runtime/`` sibling so per-agent state lands
        # inside the same repo (CI snapshots, hand-testing without
        # polluting ~/.scitex). Otherwise fall back to the runner's
        # default (~/.scitex/agent-container/runtime/).
        project_runtime = _project_runtime_root(config)
        state_root_override = project_runtime if project_runtime else None
        state_dir = _runner.state_dir_for(config.name, root=state_root_override)

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
        if state_root_override:
            argv.extend(["--state-root", str(state_root_override)])
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
        # Inbound HTTP turn endpoint: when ``spec.a2a.port`` is set the
        # runner serves POST /v1/turn so external producers (telegram,
        # other agents, ops) can drive new turns into the live SDK
        # session without restarting.
        a2a_host, a2a_port = _read_a2a_endpoint(config)
        if a2a_port is not None:
            argv.extend(["--a2a-port", str(a2a_port), "--a2a-host", a2a_host])

        if foreground:
            # Foreground mode: inherit the caller's stdio so SDK output
            # streams to the terminal, no detach, and block until the
            # runner exits. The runner's --print-stream flag mirrors
            # assistant chunks to stdout in real time.
            argv.append("--print-stream")
            proc = subprocess.Popen(argv, close_fds=True)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                # Cooperative shutdown: SIGTERM the runner, wait for it
                # to flush its STOPPING heartbeat, then re-raise so the
                # outer CLI can render its own farewell.
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.send_signal(signal.SIGKILL)
                raise
            return rc == 0

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

    def _state_dir(self, config: AgentConfig) -> Path:
        """Per-agent state dir: project-local if available, else default."""
        return _runner.state_dir_for(config.name, root=_project_runtime_root(config))

    def stop(self, config: AgentConfig) -> bool:
        """SIGTERM the runner; fall back to SIGKILL after 5 s."""
        state_dir = self._state_dir(config)
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
        state_dir = self._state_dir(config)
        pid = _runner.read_pid(state_dir)
        return pid is not None and _pid_alive(pid)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Return the last ``lines`` of the session transcript, prettified.

        Reads ``session.jsonl`` (one JSON object per turn event) and
        renders a compact human-readable view: the user mission, each
        assistant chunk, and a closing ``[result]`` summary with
        accumulated token totals. Falls back to the latest heartbeat
        if the agent hasn't started a conversation yet.
        """
        state_dir = self._state_dir(config)
        rendered = _format_session_tail(state_dir, lines)
        if rendered:
            return rendered
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


def _format_session_tail(state_dir, max_lines: int) -> str:
    """Render the tail of ``session.jsonl`` as a compact human view.

    Returns the empty string if the file is absent (caller falls back to
    the heartbeat). Each record renders as a single line keyed by type:

      [user]      mission text
      [assistant] streamed chunk
      [result]    session=<sid>  in/out/cache totals
      [error]     kind  detail
      [user_echo] (verbatim repr trimmed in the runner)
    """
    import json as _json

    transcript = state_dir / "session.jsonl"
    if not transcript.is_file():
        return ""
    try:
        with transcript.open(encoding="utf-8") as fh:
            raw = fh.readlines()[-max_lines:]
    except OSError:
        return ""
    out: list[str] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        kind = rec.get("type", "?")
        if kind == "user":
            out.append(f"[user]      {rec.get('text', '')}")
        elif kind == "assistant":
            out.append(f"[assistant] {rec.get('text', '')}")
        elif kind == "result":
            usage = rec.get("usage") or {}
            out.append(
                f"[result]    session={rec.get('session_id', '?')}  "
                f"in={usage.get('input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)} "
                f"cache_w={usage.get('cache_creation_input_tokens', 0)} "
                f"cache_r={usage.get('cache_read_input_tokens', 0)}"
            )
        elif kind == "error":
            out.append(f"[error]     {rec.get('kind', '?')}: {rec.get('detail', '')}")
        elif kind == "user_echo":
            out.append(f"[user_echo] {rec.get('raw', '')}")
        else:
            out.append(f"[{kind}]      {_json.dumps(rec, ensure_ascii=False)[:200]}")
    return "\n".join(out)


def _project_runtime_root(config: AgentConfig) -> "Path | None":
    """If the agent's YAML lives under a project-scope
    ``.scitex/agent-container/`` tree (a git repo with that subdir),
    return the sibling ``runtime/`` so per-agent state lands inside
    the same repo. Otherwise None.

    Delegates to ``scitex_config._ecosystem.local_state.find_project_scope``
    — same convention used by the slurm runtime, scitex-hpc, etc.
    In-repo test agents get in-repo state, keeping ``~/.scitex``
    clean and letting CI snapshot transcripts as build artifacts.
    """
    src = getattr(config, "config_path", "") or ""
    if not src:
        return None
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state)
        return None
    scope = local_state.find_project_scope("agent-container", start=Path(src).parent)
    return (scope / "runtime") if scope is not None else None


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


def _read_a2a_endpoint(config: AgentConfig) -> tuple[str, int | None]:
    """Read ``spec.a2a.{host,port}`` from the agent YAML.

    Returns ``(host, port)`` or ``(host, None)`` if the block is absent
    or ``port`` is unset. Defaults host to ``127.0.0.1`` so an agent
    YAML that only specifies ``port`` stays loopback-only.
    """
    config_path = getattr(config, "config_path", None)
    if not config_path:
        return ("127.0.0.1", None)
    yaml_path = Path(config_path)
    if not yaml_path.is_file():
        return ("127.0.0.1", None)
    try:
        import yaml

        v3 = yaml.safe_load(yaml_path.read_text()) or {}
    except (
        OSError,
        Exception,
    ):  # stx-allow: fallback (reason: malformed YAML degrades to no inbound port — runner still heartbeats)
        return ("127.0.0.1", None)
    spec = v3.get("spec") or {}
    a2a = spec.get("a2a") or {}
    if not isinstance(a2a, dict):
        return ("127.0.0.1", None)
    port = a2a.get("port")
    if not isinstance(port, int) or port <= 0:
        return ("127.0.0.1", None)
    host = a2a.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        host = "127.0.0.1"
    return (host, port)


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
