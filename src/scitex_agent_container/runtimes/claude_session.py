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
from .claude_md import cleanup_claude_md, setup_claude_md

__all__ = ["ClaudeSessionRuntime"]

# F-CS8 — silent SDK failure on heavy workdir/.claude/ trees.
# claude-agent-sdk auto-discovers ``<workdir>/.claude/`` at session
# start (hooks, skills, settings.local.json, agents). When that tree
# is large (or contains a hook that errors), the SDK swallows the
# error and returns 0 tokens with no log line — heartbeat fresh,
# every turn empty. Hard to debug. Emit a clear warning at start
# whenever the size exceeds this threshold.
_WORKDIR_CLAUDE_SIZE_WARN_BYTES = 10 * 1024 * 1024  # 10 MB


def _workdir_claude_size_bytes(workdir: str | None) -> int:
    """Return total size of ``<workdir>/.claude/`` in bytes, or 0.

    Symlinks are NOT followed (avoids loops; matches what the SDK's
    own discovery walks). Inaccessible files contribute 0 — this is
    a best-effort precheck, not a security audit.
    """
    if not workdir:
        return 0
    root = Path(workdir) / ".claude"
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        # stx-allow: fallback (reason: stat may fail on broken symlinks
        # or permission-denied entries; treat as 0 bytes rather than abort)
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            continue
    return total


def _warn_if_heavy_workdir_claude(config: AgentConfig) -> None:
    """Print stderr warning if ``<workdir>/.claude/`` is large enough
    to risk silent SDK discovery failure (F-CS8).

    Best-effort: silent for stub configs, remote configs, or workdirs
    that don't carry a ``.claude/`` subtree.
    """
    workdir = getattr(config, "expanded_workdir", None) or getattr(
        config, "workdir", None
    )
    size = _workdir_claude_size_bytes(workdir)
    if size <= _WORKDIR_CLAUDE_SIZE_WARN_BYTES:
        return
    mb = size / (1024 * 1024)
    print(
        f"warning: '{workdir}/.claude/' is {mb:.1f} MB — "
        "claude-agent-sdk auto-discovery may swallow errors and the "
        "agent will return 0 tokens per turn with no log line. "
        "Recommend a project-specific workdir (e.g. "
        "/home/<you>/proj/<this-project>/) or /tmp/<scratch>/, then "
        "reference other repos via absolute paths. (F-CS8)",
        file=sys.stderr,
        flush=True,
    )


class ClaudeSessionRuntime(RuntimeBase):
    """Daemon-mode runtime backed by ``claude-agent-sdk`` (Phase 1: heartbeat only)."""

    def _setup_workspace(self, config: AgentConfig) -> None:
        """Materialise CLAUDE.md before launching the SDK runner.

        Mirrors what ``runtimes.claude_code.ClaudeCodeRuntime`` does for
        the CLI runtime: writes ``<workdir>/.claude/CLAUDE.md`` with an
        agent-container managed section that lists the agent's HARD
        skills (``spec.skills.required[]`` → ``@<path>`` lines, eagerly
        inlined by the SDK) and SOFT skills (``spec.skills.available[]``
        → reference listing, agent reads on demand). See F-CS1.

        Best-effort: skipped for remote configs (the workdir lives on
        the remote host) and for stub configs that don't carry the full
        AgentConfig surface (unit-test SimpleNamespace fixtures).
        """
        if _is_remote_config(config):
            return
        # Stub configs (e.g. SimpleNamespace in argv-composition tests)
        # don't carry the full surface ``setup_claude_md`` walks. Detect
        # via the structural attributes the helper actually touches.
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        setup_claude_md(config, config.expanded_workdir)

    def _cleanup_workspace(self, config: AgentConfig) -> None:
        """Remove the agent-container CLAUDE.md section on stop.

        Symmetric to ``_setup_workspace``. Same defensive guards: skip
        on remote configs and on stub configs that don't carry the full
        surface.
        """
        if _is_remote_config(config):
            return
        required_attrs = ("expanded_workdir", "skills", "claude", "env", "labels")
        if not all(hasattr(config, a) for a in required_attrs):
            return
        cleanup_claude_md(config, config.expanded_workdir)

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
            # Materialise CLAUDE.md even in dry-run so callers can
            # inspect what the SDK runner would see at session start.
            self._setup_workspace(config)
            return True

        # Materialise CLAUDE.md BEFORE spawning the SDK runner so the
        # SDK's auto-load picks up `@-import` lines for required skills
        # and the soft listing for available skills (F-CS1).
        self._setup_workspace(config)

        # F-CS8: warn if workdir/.claude/ is heavy enough to risk
        # silent SDK auto-discovery failure (heartbeat fresh, every
        # turn returns 0 tokens). Best-effort precheck.
        _warn_if_heavy_workdir_claude(config)

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
            remote = getattr(config, "remote", None)
            if remote is not None and getattr(remote, "is_remote", False):
                # Layer 1: dispatch the runner over ssh. The remote-launch
                # helper renders a bash script that sources the per-host
                # hook (~/.scitex/agent-container/hosts/$(hostname).sh) and
                # exec's the runner. We pipe via `bash -l -s` so the
                # remote login shell sources .bashrc (Lmod, venv PATH,
                # etc.) before the hook runs. Daemon mode + lifecycle
                # over ssh is Layer 2 work.
                proc = _ssh_foreground_dispatch(config, argv)
            else:
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

        if _is_remote_config(config):
            # Layer 2: daemon mode over ssh. Render the launch script
            # in detach mode (setsid + nohup → emits remote PID on
            # stdout) and pipe it via ssh; the remote PID returned tells
            # us the runner survived ssh disconnection. Subsequent
            # is_running / stop / logs ssh in to inspect remote state.
            return _ssh_daemon_start(config, argv)

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
        if _is_remote_config(config):
            return _ssh_stop(config)
        state_dir = self._state_dir(config)
        pid = _runner.read_pid(state_dir)
        if pid is None:
            # Nothing to kill, but still scrub the workspace section so
            # a yaml that was never started but had setup_claude_md run
            # (e.g. dry-run materialisation) leaves no orphan markers.
            self._cleanup_workspace(config)
            return True

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._cleanup_state(state_dir)
            self._cleanup_workspace(config)
            return True
        except PermissionError:
            return False

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _pid_alive(pid):
                self._cleanup_state(state_dir)
                self._cleanup_workspace(config)
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
            self._cleanup_workspace(config)
            return True
        return False

    def is_running(self, config: AgentConfig) -> bool:
        """True if the recorded PID exists and the process is alive."""
        if _is_remote_config(config):
            return _ssh_is_running(config)
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
        if _is_remote_config(config):
            return _ssh_logs(config, lines)
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


# --- Layer 2: ssh lifecycle helpers ---------------------------------------


def _is_remote_config(config: AgentConfig) -> bool:
    """Defensive remote-detection that survives stub configs in tests."""
    remote = getattr(config, "remote", None)
    return remote is not None and getattr(remote, "is_remote", False)


def _build_ssh_command(
    config: AgentConfig, *, remote_cmd: str | None = None
) -> list[str]:
    """Build an ssh command to ``config.remote`` (no remote command appended).

    If ``remote_cmd`` is given, append ``bash -l -c <remote_cmd>`` so the
    remote login shell sources .bashrc; otherwise leave the trailing args
    off so the caller can append e.g. ``bash -l -s`` for stdin-piping.
    """
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if config.remote.hops:
        from ._ssh_chain import render_ssh_chain, skip_local_hops

        cmd.extend(render_ssh_chain(skip_local_hops(config.remote.hops)))
    else:
        if config.remote.key:
            cmd.extend(["-i", config.remote.key])
        if config.remote.port != 22:
            cmd.extend(["-p", str(config.remote.port)])
        target = (
            f"{config.remote.user}@{config.remote.host}"
            if config.remote.user
            else config.remote.host
        )
        cmd.append(target)
    if remote_cmd is not None:
        # ssh joins argv with spaces and passes the result to the remote
        # shell as one big string, so a multi-token command argument to
        # `bash -l -c` would get torn apart. Wrap the command in single
        # quotes so ssh's join leaves it intact.
        import shlex as _shlex

        cmd.extend(["bash", "-l", "-c", _shlex.quote(remote_cmd)])
    return cmd


def _remote_state_path(name: str) -> str:
    """Bash expression for the per-agent state dir on the remote.

    Uses the remote's ``$HOME`` at runtime so we don't hard-code paths.
    The runner default is ``~/.scitex/agent-container/runtime/<name>``
    unless the per-host hook exports ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``.
    """
    return f'"$HOME/.scitex/agent-container/runtime/{name}"'


def _ssh_exec_command(config: AgentConfig, remote_cmd: str, *, timeout: int = 30):
    """Run a shell snippet on the remote in a login shell."""
    return subprocess.run(
        _build_ssh_command(config, remote_cmd=remote_cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ssh_is_running(config: AgentConfig) -> bool:
    """ssh + read remote pid file + ``kill -0`` on remote PID."""
    state = _remote_state_path(config.name)
    cmd = (
        f"PID=$(cat {state}/pid 2>/dev/null) || exit 1; "
        'kill -0 "$PID" 2>/dev/null && echo alive || exit 1'
    )
    try:
        res = _ssh_exec_command(config, cmd, timeout=15)
    except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: ssh down → treat as not-running rather than crash status callers)
        return False
    return "alive" in (res.stdout or "")


def _ssh_stop(config: AgentConfig) -> bool:
    """ssh + SIGTERM the remote pid; SIGKILL after 5s; clean state dir."""
    state = _remote_state_path(config.name)
    cmd = (
        f"PID=$(cat {state}/pid 2>/dev/null); "
        '[ -z "$PID" ] && exit 0; '
        'kill -TERM "$PID" 2>/dev/null; '
        "for i in $(seq 1 50); do "
        '  kill -0 "$PID" 2>/dev/null || break; '
        "  sleep 0.1; "
        "done; "
        'kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null; '
        f"rm -f {state}/pid {state}/heartbeat.json 2>/dev/null; "
        "echo done"
    )
    try:
        res = _ssh_exec_command(config, cmd, timeout=20)
    except (
        subprocess.TimeoutExpired
    ):  # stx-allow: fallback (reason: hung remote treated as stop-failed)
        return False
    return "done" in (res.stdout or "")


def _ssh_logs(config: AgentConfig, lines: int) -> str:
    """ssh + cat remote session.jsonl tail; render inline."""
    state = _remote_state_path(config.name)
    raw_tail = lines * 4
    cmd = (
        f"if [ -f {state}/session.jsonl ]; then "
        f"  tail -n {raw_tail} {state}/session.jsonl; "
        f"elif [ -f {state}/heartbeat.json ]; then "
        f'  echo "===HEARTBEAT==="; cat {state}/heartbeat.json; '
        f'else echo "===EMPTY==="; fi'
    )
    try:
        res = _ssh_exec_command(config, cmd, timeout=15)
    except subprocess.TimeoutExpired:
        return f"(ssh timeout reading remote logs from {config.remote.host})"
    text = res.stdout or ""
    if "===EMPTY===" in text:
        return "(no session.jsonl yet on remote)"
    if text.startswith("===HEARTBEAT==="):
        return text.replace("===HEARTBEAT===", "").strip()
    import json as _json

    out_lines: list[str] = []
    kept = text.strip().split("\n")[-lines:]
    for raw in kept:
        try:
            ev = _json.loads(raw)
        except (ValueError, _json.JSONDecodeError):
            continue
        kind = ev.get("type", "?")
        if kind == "user":
            out_lines.append(f"[user] {ev.get('text', '')}")
        elif kind == "assistant":
            out_lines.append(f"[assistant] {ev.get('text', '')}")
        elif kind == "result":
            usage = ev.get("usage") or {}
            out_lines.append(
                f"[result] sess={ev.get('session_id', '?')} "
                f"in={usage.get('input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)}"
            )
        elif kind == "error":
            out_lines.append(f"[error/{ev.get('kind', '?')}] {ev.get('detail', '')}")
    return "\n".join(out_lines) if out_lines else "(no parseable events on remote)"


def _ssh_daemon_start(config: AgentConfig, runner_argv: list[str]) -> bool:
    """Launch the runner detached on the remote; verify pid file lands."""
    from .._runners._remote_launch import render_remote_launch

    remote_argv = list(runner_argv)
    if remote_argv and remote_argv[0] == sys.executable:
        remote_argv[0] = "python3"
    script = render_remote_launch(
        runner_argv=remote_argv,
        agent_name=config.name,
        state_root=None,
        detach=True,
    )
    ssh_cmd = _build_ssh_command(config) + ["bash", "-l", "-s"]
    try:
        proc = subprocess.run(
            ssh_cmd,
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0:
        return False
    # The script's `echo $!` reports the bash subshell's last-backgrounded
    # PID, which doesn't always equal the Python runner's getpid() once
    # nohup/setsid + fork+exec land. Don't enforce match — just verify
    # the runner wrote *some* pid file and a live process exists behind
    # it. The runner writes its own canonical pid via getpid().
    state = _remote_state_path(config.name)
    check_cmd = (
        "for i in $(seq 1 50); do "
        f"  P=$(cat {state}/pid 2>/dev/null); "
        '  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo ok; exit 0; fi; '
        "  sleep 0.1; "
        "done; "
        "echo nopid"
    )
    try:
        check = _ssh_exec_command(config, check_cmd, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return "ok" in (check.stdout or "")


def _ssh_foreground_dispatch(
    config: AgentConfig, runner_argv: list[str]
) -> subprocess.Popen:
    """Spawn the runner on a remote host via ssh, foreground-streaming.

    Renders a bash script via ``_remote_launch.render_remote_launch`` and
    pipes it to ``ssh <host> 'bash -l -s'``. The ssh subprocess inherits
    the caller's stdio so the runner's ``--print-stream`` chunks land on
    the operator's terminal in real time.

    Layer 1 of the remote-agent rollout for orochi consumption: the
    minimum to spawn one remote runner. Daemon-mode + ``is_running`` /
    ``stop`` / ``logs`` over ssh land in Layer 2.
    """
    from .._runners._remote_launch import render_remote_launch

    # Replace the local Python interpreter with a portable invocation
    # so the remote host's PATH (set by .bashrc + per-host hook) picks
    # the right `python`. The runner module is invoked via `-m` against
    # whatever Python the remote venv resolves.
    remote_argv = list(runner_argv)
    if remote_argv and remote_argv[0] == sys.executable:
        remote_argv[0] = "python3"

    script = render_remote_launch(
        runner_argv=remote_argv,
        agent_name=config.name,
        state_root=None,  # remote uses its own SCITEX_AGENT_CONTAINER_RUNTIME_DIR
        detach=False,  # foreground — exec the runner, stream stdio back via ssh
    )

    # Build the ssh command. We use the chain (hops) form when set;
    # fall back to plain {user@host} otherwise. ``bash -l -s`` ensures
    # the remote login shell sources .bashrc (Lmod, pyenv, venv PATH)
    # before the per-host hook fires.
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if config.remote.hops:
        from ._ssh_chain import render_ssh_chain, skip_local_hops

        ssh_cmd.extend(render_ssh_chain(skip_local_hops(config.remote.hops)))
    else:
        if config.remote.key:
            ssh_cmd.extend(["-i", config.remote.key])
        if config.remote.port != 22:
            ssh_cmd.extend(["-p", str(config.remote.port)])
        target = (
            f"{config.remote.user}@{config.remote.host}"
            if config.remote.user
            else config.remote.host
        )
        ssh_cmd.append(target)
    ssh_cmd.extend(["bash", "-l", "-s"])

    proc = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.PIPE,
        close_fds=True,
        # stdout / stderr inherit so streaming reaches the terminal
    )
    # Pipe the script body and close stdin so the remote bash exec's
    # immediately. wait() is called by the caller.
    if proc.stdin is not None:
        proc.stdin.write(script.encode("utf-8"))
        proc.stdin.close()
    return proc


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
    ``sac agent status`` to claim such processes are alive — explicitly
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
