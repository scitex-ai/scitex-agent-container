"""Canonical sbatch wrapper generator for head-spartan on Spartan HPC.

Root cause (todo#425, 2026-04-14): the hand-rolled sbatch wrapper on
Spartan ended in five consecutive silent 15-36 second short-exits
(jobs 23914805, 23916429, 23934176, 23936232, 23936277), each burning
a multi-day SLURM allocation within seconds of scheduler dispatch.

Post-mortem on spartan:~/head_spartan_*.sh found three sibling scripts
with drift between them — `head_spartan_fresh.sh` holds via
``exec sleep 604800``, `head_spartan_restart.sh` holds via a
``while tmux has-session`` loop, and `head_spartan_restart2.sh` has
no hold block at all and falls off the end of the script after the
``=== PROCESSES ===`` diagnostic block. The `restart2` drop-through
is the short-exit path: the wrapper ends normally, ``set -e`` exits
clean, SLURM reaps the cgroup, and the detached ``tmux new-session -d``
dies with it. No ``set -euo pipefail``, no ``exec > log``, no
unconditional hold.

This module is the canonical source of truth. It emits a hardened
sbatch body that:

1. Redirects stdout+stderr into ``~/slurm_logs/${SLURM_JOB_ID}.out``
   with ``set -x`` so any future short-exit is diagnosable from the
   log alone, without having to re-submit.
2. Runs under ``set -euo pipefail`` so failures crash loud rather
   than fall through to a clean exit.
3. Holds the allocation with ``tail -f /dev/null`` unconditionally,
   guarded only by a ``SCITEX_SPARTAN_DIAGNOSTIC`` opt-in env var
   (default off) for the capture-and-exit debug branch. The
   persistent hold is the DEFAULT, not the exception.
4. Never runs ``tmux new-session -d`` as the last meaningful command
   without an unconditional downstream ``wait`` or ``tail -f``.

The hardeners here are the regression surface for
``tests/test_sbatch_spartan.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# Required hardeners — these strings are asserted by the regression test.
# Changing them here without updating the test is a deliberate decision.
# ---------------------------------------------------------------------------

REQUIRED_SHEBANG = "#!/bin/bash"
REQUIRED_STRICT_MODE = "set -euo pipefail"
REQUIRED_LOG_REDIRECT = 'exec > "${HOME}/slurm_logs/${SLURM_JOB_ID:-nojob}.out" 2>&1'
REQUIRED_XTRACE = "set -x"
REQUIRED_HOLD = "tail -f /dev/null"
DIAGNOSTIC_ENV_VAR = "SCITEX_SPARTAN_DIAGNOSTIC"


@dataclass
class SpartanSbatchConfig:
    """Inputs for the head-spartan sbatch wrapper.

    Defaults reflect the 2026-04-14 production config for
    head-spartan on the ``sapphire`` partition. Override per call
    site rather than editing the defaults.
    """

    job_name: str = "head-spartan"
    partition: str = "sapphire"
    time_limit: str = "7-00:00:00"
    cpus_per_task: int = 2
    mem: str = "4G"
    agent_id: str = "head-spartan"
    agent_role: str = "head"
    orochi_channels: str = (
        "#neurovista,#ywatanabe,#general,#agent,#progress,#escalation"
    )
    workdir: str = "$HOME/.scitex/orochi/workspaces/head-spartan"
    claude_bin: str = "$HOME/.npm-global/bin/claude"
    claude_model: str = "opus[1m]"
    extra_add_dirs: List[str] = field(
        default_factory=lambda: [
            "$HOME/proj/scitex-agent-container/src/scitex_agent_container/_skills/",
            "$HOME/proj/scitex-orochi/src/scitex_orochi/_skills/",
            "$HOME/.scitex/orochi/skills/",
        ]
    )
    # Extra safety buffer: even if tail -f /dev/null somehow exits,
    # fall through to a final abort with a clear error.
    trap_on_exit: bool = True


def _sbatch_directives(cfg: SpartanSbatchConfig) -> List[str]:
    return [
        f"#SBATCH --partition={cfg.partition}",
        f"#SBATCH --time={cfg.time_limit}",
        f"#SBATCH --cpus-per-task={cfg.cpus_per_task}",
        f"#SBATCH --mem={cfg.mem}",
        f"#SBATCH --job-name={cfg.job_name}",
        "#SBATCH --output=/home/ywatanabe/slurm_logs/%x_%j.out",
        "#SBATCH --error=/home/ywatanabe/slurm_logs/%x_%j.err",
    ]


def _claude_invocation(cfg: SpartanSbatchConfig) -> str:
    add_dirs = " ".join(f"--add-dir {d}" for d in cfg.extra_add_dirs)
    return (
        f"exec {cfg.claude_bin} "
        f"--continue "
        f"--model {cfg.claude_model} "
        f"--dangerously-skip-permissions "
        f"--dangerously-load-development-channels server:scitex-orochi "
        f"{add_dirs}"
    )


def render_sbatch_script(cfg: SpartanSbatchConfig | None = None) -> str:
    """Render the full hardened sbatch wrapper script as a string.

    The returned string is suitable for writing to
    ``~/head_spartan_sbatch.sh`` on spartan and submitting via
    ``sbatch ~/head_spartan_sbatch.sh``.

    All five hardeners from todo#425 are applied unconditionally:

    * ``set -euo pipefail`` at the top
    * ``exec > slurm_logs/<jobid>.out 2>&1 ; set -x`` early redirect
    * ``trap 'echo ... ; exit 1' EXIT`` so a drop-through fails loud
    * ``tmux new-session -d`` kept for spawn, but followed by an
      unconditional ``tail -f /dev/null`` hold
    * Diagnostic capture-and-exit branch gated behind
      ``SCITEX_SPARTAN_DIAGNOSTIC=1`` (default off)
    """
    cfg = cfg or SpartanSbatchConfig()

    directives = "\n".join(_sbatch_directives(cfg))
    claude_cmd = _claude_invocation(cfg)

    trap_block = ""
    if cfg.trap_on_exit:
        trap_block = (
            '\n# Fail loud if we ever fall through the hold below.\n'
            'trap \'rc=$?; echo "[todo#425] wrapper exiting with rc=$rc at $(date -u +%FT%TZ)" >&2; '
            'exit "${rc:-1}"\' EXIT\n'
        )

    script = f"""{REQUIRED_SHEBANG}
{directives}
#
# Hardened head-spartan sbatch wrapper (todo#425).
# Canonical generator: scitex_agent_container.runtimes.sbatch_spartan
# Do NOT hand-edit on spartan — regenerate from the generator instead.

{REQUIRED_STRICT_MODE}

# Redirect everything into a per-job log before anything else can fail
# silently. This means the NEXT short-exit is diagnosable without guessing
# which log file was used.
mkdir -p "${{HOME}}/slurm_logs"
{REQUIRED_LOG_REDIRECT}
{REQUIRED_XTRACE}
{trap_block}
cd "{cfg.workdir}"
export CLAUDE_AGENT_ID="{cfg.agent_id}"
export SCITEX_OROCHI_AGENT="{cfg.agent_id}"
export CLAUDE_AGENT_ROLE="{cfg.agent_role}"
export SCITEX_OROCHI_CHANNELS="{cfg.orochi_channels}"
export CLAUDE_DISABLE_AUTO_UPDATE=1
export TMUX_TMPDIR="${{HOME}}/.tmux-sockets"
mkdir -p "${{TMUX_TMPDIR}}"

# Clean up any stale tmux server from a previous allocation.
tmux kill-server 2>/dev/null || true
sleep 1

# Spawn claude in a detached tmux session. This returns immediately;
# the hold at the bottom of the script is what actually keeps the
# SLURM allocation alive.
tmux new-session -d -s "{cfg.agent_id}" "{claude_cmd}"

# Iterative prompt dismissal (dev-channels, resume, press-enter).
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    PANE=$(tmux capture-pane -t "{cfg.agent_id}" -p 2>/dev/null | tail -40 || true)
    if echo "$PANE" | grep -q "I am using this for local development"; then
        tmux send-keys -t "{cfg.agent_id}" "1"; sleep 0.3
        tmux send-keys -t "{cfg.agent_id}" C-m
        continue
    fi
    if echo "$PANE" | grep -q "Resume from summary"; then
        tmux send-keys -t "{cfg.agent_id}" Enter
        continue
    fi
    if echo "$PANE" | grep -q "Press Enter to continue"; then
        tmux send-keys -t "{cfg.agent_id}" Enter
        continue
    fi
    if echo "$PANE" | grep -qE '\\xe2\\x9d\\xaf\\s*$'; then
        # working prompt reached
        break
    fi
done

# Optional one-shot diagnostic capture-and-exit branch. This is the
# branch that used to be the DEFAULT in older hand-edited wrappers
# and caused the 2026-04-14 silent short-exit chain (todo#425). It
# is now opt-in and will terminate the allocation on purpose.
if [[ "${{{DIAGNOSTIC_ENV_VAR}:-0}}" = "1" ]]; then
    echo "=== DIAGNOSTIC MODE ({DIAGNOSTIC_ENV_VAR}=1) ==="
    tmux capture-pane -t "{cfg.agent_id}" -p | tail -60 || true
    pgrep -af claude | grep -v pgrep | head -5 || true
    echo "=== DIAGNOSTIC MODE: exiting now (this is intentional) ==="
    exit 0
fi

# Unconditional persistent hold. `tail -f /dev/null` is idempotent
# and never decrements — unlike a multi-day literal sleep computed
# off the walltime, which can race-terminate near the walltime
# boundary. If tmux dies we still keep the allocation so the healer
# can observe and rescue from outside.
echo "[todo#425] entering persistent hold via tail -f /dev/null at $(date -u +%FT%TZ)"
{REQUIRED_HOLD}
"""
    return script


__all__ = [
    "REQUIRED_SHEBANG",
    "REQUIRED_STRICT_MODE",
    "REQUIRED_LOG_REDIRECT",
    "REQUIRED_XTRACE",
    "REQUIRED_HOLD",
    "DIAGNOSTIC_ENV_VAR",
    "SpartanSbatchConfig",
    "render_sbatch_script",
]
