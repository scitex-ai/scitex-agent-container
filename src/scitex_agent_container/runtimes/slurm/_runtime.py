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
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from scitex_config._ecosystem import local_state

from ...config import AgentConfig
from ..base import RuntimeBase
from ._render import render_sbatch_script
from ._state import _clear_state, _read_state, _write_state

logger = logging.getLogger(__name__)


class SlurmRuntime(RuntimeBase):
    """SLURM-backed runtime. Submits ``sbatch``, tracks jobid, polls
    ``squeue``, tails the per-job log, ``scancel``s on stop.
    """

    def _logs_dir(self, cfg: AgentConfig) -> Path:
        return Path(cfg.slurm.logs_dir).expanduser()

    def _sbatch_path(self, cfg: AgentConfig) -> Path:
        root = local_state.runtime_path("agent-container", "slurm-scripts")
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{cfg.name}.sbatch"

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        # Resolve helpers via the package namespace at call time so test
        # monkeypatching of ``runtimes.slurm.subprocess`` and the hpc
        # helpers takes effect (the tests patch attributes on the package,
        # not on this submodule).
        from . import _pkg_lookup as _pkg

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
        try:
            proc = _pkg().subprocess.run(
                ["sbatch", str(script_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:  # stx-allow: fallback (reason: file may not exist on first use)
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
        # Dual-write: also register a scitex-hpc Reservation lease so
        # `scitex-hpc reservations list` sees this agent and operators
        # can run ad-hoc commands inside the allocation via
        # `scitex-hpc reservations exec <name> 'cmd'`. Best-effort —
        # if scitex-hpc is missing, sac keeps working with its own state.
        _pkg()._maybe_register_hpc_reservation(config, job_id)
        logger.info("SlurmRuntime: %s -> job %s", config.name, job_id)
        return True

    def stop(self, config: AgentConfig) -> bool:
        from . import _pkg_lookup as _pkg

        state = _read_state(config.name)
        if not state:
            logger.info("SlurmRuntime: no state for %s; nothing to stop", config.name)
            return True
        job_id = str(state.get("job_id", ""))
        if not job_id:
            _clear_state(config.name)
            return True
        try:
            _pkg().subprocess.run(
                ["scancel", job_id],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError:  # stx-allow: fallback (reason: file may not exist on first use)
            logger.warning("scancel not found; cannot stop %s", config.name)
            return False
        _clear_state(config.name)
        # Best-effort: also clear the scitex-hpc Reservation lease (if any).
        # The SLURM job is already gone via scancel above; just remove the
        # state file. Avoids stale entries in `scitex-hpc reservations list`.
        _pkg()._maybe_clear_hpc_reservation(config.name)
        return True

    def is_running(self, config: AgentConfig) -> bool:
        from . import _pkg_lookup as _pkg

        state = _read_state(config.name)
        if not state:
            return False
        job_id = str(state.get("job_id", ""))
        if not job_id:
            return False
        try:
            proc = _pkg().subprocess.run(
                ["squeue", "-j", job_id, "-h", "-o", "%T"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError:  # stx-allow: fallback (reason: file may not exist on first use)
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
        try:
            proc = subprocess.run(
                ["tail", "-n", str(lines), str(log_file)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return proc.stdout
        except FileNotFoundError:  # stx-allow: fallback (reason: file may not exist on first use)
            return log_file.read_text()[-4096:]


_SBATCH_JOBID_RE = re.compile(r"Submitted batch job\s+(\d+)")


def _parse_sbatch_jobid(stdout: str) -> str:
    """Extract the numeric jobid from ``sbatch`` stdout.

    Canonical form is ``"Submitted batch job 12345"``.
    """
    m = _SBATCH_JOBID_RE.search(stdout or "")
    return m.group(1) if m else ""


def _maybe_register_hpc_reservation(cfg: AgentConfig, job_id: str) -> None:
    """Best-effort: write a ``scitex-hpc`` Reservation lease for this agent.

    Operators with ``scitex-hpc>=0.5.0`` installed gain the
    ``scitex-hpc reservations {list,exec,attach,refresh}`` CLI surface
    against agents started here. Without scitex-hpc installed, sac falls
    back to its own state file silently.

    Sac runs sbatch locally on the SLURM submission host (login node), so
    the reservation host is recorded as ``localhost`` — operators
    invoking ``scitex-hpc`` from the same login node will find the lease.
    """
    try:
        from scitex_hpc import Reservation  # type: ignore
    except ImportError:  # stx-allow: fallback (reason: optional dependency not installed)
        return
    try:
        Reservation.from_jobid(
            host="localhost",
            job_id=str(job_id),
            name=cfg.name,
            persistent=bool(cfg.slurm.auto_resubmit),
            refresh_node=False,  # squeue probe over loopback adds no value
        )
    except FileExistsError:  # stx-allow: fallback (reason: expected failure — see inline comment)
        # Lease already present — typical on a force-restart. Leave it
        # alone; sac's primary state file is the source of truth.
        pass
    # stx-allow: fallback (reason: optional scitex-hpc integration; registration failure must not abort job submission)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "scitex-hpc Reservation registration failed for %s: %s",
            cfg.name,
            exc,
        )


def _maybe_clear_hpc_reservation(agent_name: str) -> None:
    """Best-effort: remove a scitex-hpc Reservation lease state file.

    Mirror of :func:`_maybe_register_hpc_reservation`. The scancel itself
    is owned by sac (already done by the caller); this just clears the
    lease's on-disk state so it doesn't show up in `scitex-hpc
    reservations list` after the agent stops.
    """
    try:
        from scitex_hpc import Reservation  # type: ignore
    except ImportError:  # stx-allow: fallback (reason: optional dependency not installed)
        return
    # stx-allow: fallback (reason: optional scitex-hpc integration; lease cleanup failure must not block agent shutdown)
    try:
        res = Reservation.get(agent_name, host="localhost")
        if res is not None:
            # missing_ok=True so an already-cancelled job doesn't raise
            res.release(missing_ok=True)
    except Exception as exc:  # pragma: no cover — defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning(
            "scitex-hpc Reservation cleanup failed for %s: %s", agent_name, exc
        )


__all__ = [
    "SlurmRuntime",
    "_maybe_clear_hpc_reservation",
    "_maybe_register_hpc_reservation",
    "_parse_sbatch_jobid",
]
