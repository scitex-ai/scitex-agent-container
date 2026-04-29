"""Generic SLURM runtime adapter.

Wraps an agent in an ``sbatch`` allocation. Submission happens on the host
this module runs on — typically a SLURM submission host (login node). The
job body spawns the agent in a tmux session on the allocated compute node,
holds the allocation with ``tail -f /dev/null``, and auto-resubmits itself
one hour before walltime via a ``SIGUSR1`` trap.

Architectural contract: this runtime is **orochi-agnostic and fleet-
agnostic**. External orchestrators plug in through shell-fragment hook
paths declared on ``spec.slurm.hooks``. Hooks are *sourced* (not exec'd)
so they can mutate the wrapper's env — required for ``module load`` on
Lmod-based HPC clusters, ``LD_LIBRARY_PATH`` adjustments, and exporting
consumer-specific env vars (e.g. ``SCITEX_OROCHI_*``) into the agent's
process.

Hook env vars set before each hook is sourced:
    SAC_AGENT_ID   — effective agent id (e.g. ``head-spartan``)
    SAC_JOB_ID     — ``$SLURM_JOB_ID`` (unset during ``pre_submit``)
    SAC_WORKDIR    — agent workspace path
    SAC_LOG_FILE   — ``<logs_dir>/<jobid>.out``
    SAC_PHASE      — ``pre_submit`` | ``pre_agent`` | ``walltime_signal`` |
                     ``post_agent`` | ``attach``

Hardener strings at module top are the regression surface for
``tests/test_slurm_runtime.py``. Changing them without updating the test
is a deliberate decision.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardeners — promoted from the retired sbatch_spartan.py (todo#425) and
# llama-on-slurm's walltime-auto-resubmit pattern. These strings are
# asserted by the regression test.
# ---------------------------------------------------------------------------

REQUIRED_SHEBANG = "#!/bin/bash"
REQUIRED_STRICT_MODE = "set -euo pipefail"
REQUIRED_XTRACE = "set -x"
REQUIRED_HOLD_DEFAULT = "tail -f /dev/null"
REQUIRED_EXIT_TRAP_MARKER = "[sac/slurm] wrapper exiting"
REQUIRED_USR1_TRAP_MARKER = "_sac_slurm_walltime_handler"
# Heartbeat loop markers — asserted by tests and by operators grepping
# job logs ("is my compute-node heartbeat even running?"). Present only
# when ``spec.slurm.heartbeat.command`` is non-empty.
HEARTBEAT_LOOP_MARKER = "_sac_slurm_heartbeat_loop"
HEARTBEAT_START_MARKER = "[sac/slurm] heartbeat daemon started"


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

_STATE_DIR_ENV = "SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR"


def _state_dir() -> Path:
    default = Path.home() / ".scitex" / "agent-container" / "slurm-state"
    return Path(os.environ.get(_STATE_DIR_ENV, str(default)))


def _state_path(name: str) -> Path:
    return _state_dir() / f"{name}.json"


def _write_state(name: str, data: dict) -> None:
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    _state_path(name).write_text(json.dumps(data, indent=2))


def _read_state(name: str) -> dict | None:
    p = _state_path(name)
    if not p.exists():
        return None
    # stx-allow: fallback (reason: state file may be corrupt or unreadable)
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clear_state(name: str) -> None:
    p = _state_path(name)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Script rendering
# ---------------------------------------------------------------------------


def _build_claude_command(cfg: AgentConfig) -> str:
    """Build the claude-code invocation from the agent's claude spec.

    Mirrors the shape ClaudeCodeRuntime uses for its tmux session. Kept
    deliberately minimal and consumer-agnostic: any consumer-specific flags
    (e.g. ``--dangerously-load-development-channels``) are passed in via
    ``config.claude.flags`` from the agent YAML. ``claude`` is resolved
    via ``PATH`` — HPC hooks can prepend ``module load nodejs`` etc.
    """
    parts = ["claude"]

    session_mode = getattr(cfg.claude, "session", "continue-or-new")
    if session_mode in ("continue", "continue-or-new"):
        parts.append("--continue")

    model = cfg.model or "sonnet"
    parts.extend(["--model", model])

    flags = list(getattr(cfg.claude, "flags", []) or [])
    for flag in flags:
        parts.append(str(flag))

    # Command is embedded inside a double-quoted tmux new-session argument.
    return " ".join(parts)


def _hook_source(path: str, phase: str, agent_id: str, logs_dir: str) -> str:
    """Emit a shell fragment that sources ``path`` if it exists.

    Empty ``path`` emits an empty string so the wrapper stays clean.
    """
    if not path:
        return ""
    return (
        f"\n# Hook: {phase}\n"
        f'if [[ -f "{path}" ]]; then\n'
        f'    SAC_AGENT_ID="{agent_id}" \\\n'
        f'    SAC_WORKDIR="${{SAC_WORKDIR:-}}" \\\n'
        f'    SAC_LOG_FILE="{logs_dir}/${{SLURM_JOB_ID:-nojob}}.out" \\\n'
        f'    SAC_JOB_ID="${{SLURM_JOB_ID:-}}" \\\n'
        f'    SAC_PHASE="{phase}" \\\n'
        f'    source "{path}"\n'
        f"fi\n"
    )


def _heartbeat_block(cfg: AgentConfig, logs_dir: str) -> str:
    """Emit a shell block that spawns a compute-node heartbeat daemon.

    Returns an empty string when ``spec.slurm.heartbeat.command`` is empty
    (opt-in). When enabled, the block:

    * Starts a backgrounded ``while true; do ...; sleep N; done`` loop on
      the compute node, in parallel with the tmux session that runs
      claude-code.
    * Redirects its stdout/stderr to a stable log file (defaults to
      ``<logs_dir>/<jobid>.heartbeat.log``) so operators can diagnose
      push failures without attaching to the job.
    * Records the loop PID so the EXIT trap can clean it up — no
      zombie pushers linger after the wrapper tears down.
    * Uses ``setsid`` when available so the loop survives a stray
      ``SIGHUP`` from tmux restarts.

    Root cause this solves: host-level heartbeat daemons (systemd user
    timers on Linux, launchd on macOS) installed by orochi's
    ``bootstrap-host.sh`` run on the *login node* of HPC clusters. They
    enumerate local tmux sessions via ``_list_local_agents()`` which
    ``subprocess.run(["tmux", "list-sessions"])`` — invisible to the
    compute node's tmux daemon. The hub therefore never receives a
    heartbeat for agents launched through the SLURM runtime and marks
    them dead after ~5 minutes (lead msg#15654, head-spartan).
    """
    hb = cfg.slurm.heartbeat
    cmd = (hb.command or "").strip()
    if not cmd:
        return ""

    log_file = hb.log_file.strip()
    if log_file:
        log_file = str(Path(log_file).expanduser())
    else:
        log_file = f"{logs_dir}/${{SLURM_JOB_ID:-nojob}}.heartbeat.log"

    interval = max(1, int(hb.interval_s))

    # setsid detaches the loop from the wrapper's session so SIGHUP from
    # tmux server teardown doesn't cascade into the pusher. Fall back to
    # plain background when setsid is missing (BusyBox, some minimal
    # HPC images).
    return f"""
# ---------------------------------------------------------------------------
# Compute-node heartbeat daemon (spec.slurm.heartbeat)
# ---------------------------------------------------------------------------
# Loops the configured push command every {interval}s so the hub sees the
# agent as alive. The login-node systemd timer can't reach compute-node
# tmux sessions, so this loop is the only live signal the hub will ever
# receive for this job.
{HEARTBEAT_LOOP_MARKER}() {{
    while true; do
        {cmd} || true
        sleep {interval}
    done
}}
mkdir -p "$(dirname "{log_file}")"
if command -v setsid >/dev/null 2>&1; then
    setsid bash -c '{HEARTBEAT_LOOP_MARKER}() {{ while true; do {cmd} || true; sleep {interval}; done; }}; {HEARTBEAT_LOOP_MARKER}' \\
        >> "{log_file}" 2>&1 &
else
    ( {HEARTBEAT_LOOP_MARKER} ) >> "{log_file}" 2>&1 &
fi
export SAC_HEARTBEAT_PID=$!
echo "{HEARTBEAT_START_MARKER} pid=${{SAC_HEARTBEAT_PID}} interval={interval}s log={log_file}"
"""


def _sbatch_directives(cfg: AgentConfig) -> list[str]:
    slurm = cfg.slurm
    job_name = slurm.job_name or cfg.name
    # Expand ~ here: bash does NOT expand ~ inside double-quoted strings,
    # and every path in the emitted script is double-quoted for safety.
    logs_dir = str(Path(slurm.logs_dir).expanduser())

    directives = [f"#SBATCH --job-name={job_name}"]
    if slurm.partition:
        directives.append(f"#SBATCH --partition={slurm.partition}")
    directives.extend(
        [
            f"#SBATCH --time={slurm.time_limit}",
            f"#SBATCH --nodes={slurm.nodes}",
            f"#SBATCH --ntasks={slurm.ntasks}",
            f"#SBATCH --cpus-per-task={slurm.cpus_per_task}",
            f"#SBATCH --mem={slurm.mem}",
        ]
    )
    if slurm.gres:
        directives.append(f"#SBATCH --gres={slurm.gres}")
    if slurm.signal:
        directives.append(f"#SBATCH --signal={slurm.signal}")
    directives.append(f"#SBATCH --output={logs_dir}/%x_%j.out")
    directives.append(f"#SBATCH --error={logs_dir}/%x_%j.err")
    directives.extend(slurm.extra_directives)
    return directives


def render_sbatch_script(cfg: AgentConfig) -> str:
    """Render the full sbatch wrapper script as a string.

    The returned text is suitable for writing to a file and submitting via
    ``sbatch <file>``. All hardeners and plugin hook ports are applied.
    """
    slurm = cfg.slurm
    agent_id = cfg.name
    # Expand ~ at render time — bash does not expand ~ inside the
    # double-quoted strings we emit into the wrapper (cd "$workdir", exec
    # > "$logs_dir/...", hook SAC_* env).
    workdir = str(
        Path(
            cfg.workdir or f"~/.scitex/agent-container/workspaces/{agent_id}"
        ).expanduser()
    )
    logs_dir = str(Path(slurm.logs_dir).expanduser())
    tmux_session = cfg.screen_name or agent_id
    claude_cmd = _build_claude_command(cfg)

    directives = "\n".join(_sbatch_directives(cfg))

    pre_submit = _hook_source(slurm.hooks.pre_submit, "pre_submit", agent_id, logs_dir)
    pre_agent = _hook_source(slurm.hooks.pre_agent, "pre_agent", agent_id, logs_dir)
    walltime_hook = _hook_source(
        slurm.hooks.walltime_signal, "walltime_signal", agent_id, logs_dir
    )
    post_agent = _hook_source(slurm.hooks.post_agent, "post_agent", agent_id, logs_dir)
    heartbeat_block = _heartbeat_block(cfg, logs_dir)

    resubmit_line = (
        'sbatch "$0"'
        if slurm.auto_resubmit
        else 'echo "[sac/slurm] auto_resubmit disabled"'
    )

    script = f"""{REQUIRED_SHEBANG}
{directives}
#
# Generic sac SLURM wrapper. Generated by
# scitex_agent_container.runtimes.slurm.render_sbatch_script — do not
# hand-edit on the compute host; regenerate via ``sac render-sbatch``.

{REQUIRED_STRICT_MODE}

# pre_submit runs BEFORE the wrapper continues into the job body. When this
# script is invoked under sbatch, SLURM_JOB_ID is already set by SLURM — so
# in practice pre_submit and pre_agent run on the same node (the compute
# node). External orchestrators who need a true "login-node pre-submit"
# should perform that side-effect in their own submit wrapper before
# calling sbatch. This hook still runs here for symmetry with the phase
# taxonomy.
{pre_submit}
mkdir -p "{logs_dir}"
exec > "{logs_dir}/${{SLURM_JOB_ID:-nojob}}.out" 2>&1
{REQUIRED_XTRACE}

# Fail loud on drop-through: if we ever exit the hold below unexpectedly,
# this trap surfaces the return code rather than the scheduler silently
# reaping the job. Also tears down the optional heartbeat daemon so
# compute-node pushers never outlive the allocation.
trap 'rc=$?; kill "${{SAC_HEARTBEAT_PID:-0}}" 2>/dev/null || true; echo "{REQUIRED_EXIT_TRAP_MARKER} rc=$rc at $(date -u +%FT%TZ)" >&2; exit "${{rc:-1}}"' EXIT

# Walltime auto-resubmit: SLURM fires SIGUSR1 ``@3600`` seconds before
# walltime (see --signal directive). The handler sources the external
# walltime_signal hook (for e.g. hub notifications) and then resubmits
# this exact script so the allocation is seamlessly replaced.
{REQUIRED_USR1_TRAP_MARKER}() {{
    echo "[sac/slurm] walltime signal received at $(date -u +%FT%TZ)"
    {walltime_hook.strip() if walltime_hook.strip() else "true  # no walltime_signal hook"}
    {resubmit_line}
}}
trap {REQUIRED_USR1_TRAP_MARKER} USR1

export SAC_AGENT_ID="{agent_id}"
export SAC_WORKDIR="{workdir}"
mkdir -p "{workdir}"
cd "{workdir}"

# pre_agent hook mutates env (module load, LD_LIBRARY_PATH, consumer-
# specific exports like SCITEX_OROCHI_*). Sourced so the exports survive
# into the agent process.
{pre_agent}
# tmux socket lives under HOME to survive shared /tmp cleanups.
export TMUX_TMPDIR="${{HOME}}/.tmux-sockets"
mkdir -p "${{TMUX_TMPDIR}}"

# Clean up any stale tmux server from a previous allocation on this node.
tmux kill-server 2>/dev/null || true
sleep 1

# Spawn the agent in a detached tmux session. This returns immediately;
# the hold at the bottom of the script is what actually keeps the SLURM
# allocation alive.
tmux new-session -d -s "{tmux_session}" "{claude_cmd}"
{heartbeat_block}
# post_agent hook fires once the tmux session ends. The hold below keeps
# the job alive even if the agent dies — external observers (healers)
# can inspect and decide whether to scancel.
{post_agent}

# Persistent hold. Idempotent, never decrements — safer than a literal
# sleep computed off walltime (which can race-terminate near the
# boundary).
echo "[sac/slurm] entering persistent hold at $(date -u +%FT%TZ)"
{slurm.hold}
"""
    return script


def render_attach_command(cfg: AgentConfig, job_id: str | None = None) -> str:
    """Return a shell command that attaches to the running agent's tmux.

    Uses ``srun --jobid=$JID --pty bash -c 'tmux attach -t <session>'``
    so the operator lands on the compute node running the agent.

    If ``job_id`` is not given, reads from sac's slurm state file.
    """
    if job_id is None:
        state = _read_state(cfg.name) or {}
        job_id = state.get("job_id", "")
    session = cfg.screen_name or cfg.name

    attach_hook = cfg.slurm.hooks.attach
    pre = ""
    if attach_hook:
        pre = (
            f'SAC_AGENT_ID="{cfg.name}" SAC_JOB_ID="{job_id}" '
            f'SAC_PHASE="attach" bash "{attach_hook}" && '
        )
    return (
        f"{pre}srun --jobid={job_id} --pty bash -c "
        f"'tmux -L default attach -t {session}'"
    )


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class SlurmRuntime(RuntimeBase):
    """SLURM-backed runtime. Submits ``sbatch``, tracks jobid, polls
    ``squeue``, tails the per-job log, ``scancel``s on stop.
    """

    def _logs_dir(self, cfg: AgentConfig) -> Path:
        return Path(cfg.slurm.logs_dir).expanduser()

    def _sbatch_path(self, cfg: AgentConfig) -> Path:
        root = Path.home() / ".scitex" / "agent-container" / "slurm-scripts"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{cfg.name}.sbatch"

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        script_text = render_sbatch_script(config)
        script_path = self._sbatch_path(config)
        script_path.write_text(script_text)
        script_path.chmod(0o755)

        logs_dir = self._logs_dir(config)
        logs_dir.mkdir(parents=True, exist_ok=True)

        if self.is_running(config) and not force:
            logger.info(
                "SlurmRuntime: %s already has a live job; skipping submit",
                config.name,
            )
            return True

        if force and self.is_running(config):
            self.stop(config)

        logger.info("SlurmRuntime: submitting sbatch %s", script_path)
        # stx-allow: fallback (reason: sbatch binary may not be on PATH on non-SLURM hosts)
        try:
            proc = subprocess.run(
                ["sbatch", str(script_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "sbatch binary not found on this host. SlurmRuntime must run "
                "from a SLURM submission host (login node)."
            ) from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"sbatch failed for {config.name}:\n"
                f"  stdout: {proc.stdout.strip()}\n"
                f"  stderr: {proc.stderr.strip()}"
            )

        job_id = _parse_sbatch_jobid(proc.stdout)
        if not job_id:
            raise RuntimeError(
                f"sbatch submitted but no jobid parsed from stdout: {proc.stdout!r}"
            )

        _write_state(
            config.name,
            {
                "name": config.name,
                "job_id": job_id,
                "script": str(script_path),
                "submitted_stdout": proc.stdout.strip(),
            },
        )
        logger.info("SlurmRuntime: %s -> job %s", config.name, job_id)
        return True

    def stop(self, config: AgentConfig) -> bool:
        state = _read_state(config.name)
        if not state:
            logger.info("SlurmRuntime: no state for %s; nothing to stop", config.name)
            return True
        job_id = str(state.get("job_id", ""))
        if not job_id:
            _clear_state(config.name)
            return True
        # stx-allow: fallback (reason: scancel may not be available on non-SLURM hosts)
        try:
            subprocess.run(
                ["scancel", job_id],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            logger.warning("scancel not found; cannot stop %s", config.name)
            return False
        _clear_state(config.name)
        return True

    def is_running(self, config: AgentConfig) -> bool:
        state = _read_state(config.name)
        if not state:
            return False
        job_id = str(state.get("job_id", ""))
        if not job_id:
            return False
        # stx-allow: fallback (reason: squeue may not be available on non-SLURM hosts)
        try:
            proc = subprocess.run(
                ["squeue", "-j", job_id, "-h", "-o", "%T"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            return False
        status = (proc.stdout or "").strip()
        # PENDING / RUNNING / CONFIGURING etc. all mean "still allocated".
        # Empty output = job not in queue (completed / cancelled / never was).
        return bool(status)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        state = _read_state(config.name)
        if not state:
            return f"[sac/slurm] no submission record for {config.name}"
        job_id = str(state.get("job_id", ""))
        logs_dir = self._logs_dir(config)
        log_file = logs_dir / f"{config.slurm.job_name or config.name}_{job_id}.out"
        if not log_file.exists():
            # Fall back to the wrapper's own redirect target.
            log_file = logs_dir / f"{job_id}.out"
        if not log_file.exists():
            return f"[sac/slurm] log not found: {log_file}"
        # stx-allow: fallback (reason: tail command may not be available; fallback to direct read)
        try:
            proc = subprocess.run(
                ["tail", "-n", str(lines), str(log_file)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return proc.stdout
        except FileNotFoundError:
            return log_file.read_text()[-4096:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SBATCH_JOBID_RE = re.compile(r"Submitted batch job\s+(\d+)")


def _parse_sbatch_jobid(stdout: str) -> str:
    """Extract the numeric jobid from ``sbatch`` stdout.

    Canonical form is ``"Submitted batch job 12345"``.
    """
    m = _SBATCH_JOBID_RE.search(stdout or "")
    return m.group(1) if m else ""


__all__ = [
    "HEARTBEAT_LOOP_MARKER",
    "HEARTBEAT_START_MARKER",
    "REQUIRED_SHEBANG",
    "REQUIRED_STRICT_MODE",
    "REQUIRED_XTRACE",
    "REQUIRED_HOLD_DEFAULT",
    "REQUIRED_EXIT_TRAP_MARKER",
    "REQUIRED_USR1_TRAP_MARKER",
    "SlurmRuntime",
    "render_sbatch_script",
    "render_attach_command",
]
