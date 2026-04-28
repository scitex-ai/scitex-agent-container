"""Compute-node heartbeat shell-fragment generator.

Root cause this solves: host-level heartbeat daemons (systemd user
timers on Linux, launchd on macOS) installed by orochi's
``bootstrap-host.sh`` run on the *login node* of HPC clusters. They
enumerate local tmux sessions via ``_list_local_agents()`` which
``subprocess.run(["tmux", "list-sessions"])`` — invisible to the
compute node's tmux daemon. The hub therefore never receives a
heartbeat for agents launched through the SLURM runtime and marks
them dead after ~5 minutes (lead msg#15654, head-spartan).
"""

from __future__ import annotations

from pathlib import Path

from ...config import AgentConfig
from ._constants import HEARTBEAT_LOOP_MARKER, HEARTBEAT_START_MARKER


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


__all__ = ["_heartbeat_block"]
