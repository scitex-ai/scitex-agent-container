"""SLURM tenant runtime — share one SLURM allocation across many agents.

The motivation: HPC queue wait dominates iteration time. ``runtime: slurm``
submits a fresh sbatch per agent, paying queue wait per launch. With
``runtime: slurm-tenant`` an operator books one long-running reservation
(via ``scitex-hpc reservations book ...``) and launches many agents
*inside* that allocation via ``srun --jobid --overlap``.

Each agent is a tmux session on the compute node, owned by the same SLURM
job. ``stop`` only kills the tmux session, not the underlying job.

Compatible with the 2026-04-26 IT Security ruling: bastion-initiated SSH
only, no daemons or tunnels. Inherits scitex-hpc's policy compliance.

Example YAML::

    apiVersion: scitex-agent-container/v3
    kind: Agent
    spec:
      runtime: slurm-tenant
      slurm:
        reservation: dev-pool       # name of the existing scitex-hpc lease
      claude:
        flags: [--dangerously-skip-permissions]

Then ``sac start dev-helper`` runs ``claude`` in a fresh tmux session
inside the ``dev-pool`` reservation's allocation.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import TYPE_CHECKING

from ..config import AgentConfig
from ._ssh_chain import build_ssh_command, skip_local_hops
from .base import RuntimeBase

if TYPE_CHECKING:
    from scitex_hpc import Reservation as _ResType  # noqa: F401

logger = logging.getLogger(__name__)

_SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
]


def _import_reservation():
    """Late import so missing scitex-hpc gives a clear error, not import-time crash."""
    try:
        from scitex_hpc import Reservation  # type: ignore
    except ImportError as exc:  # stx-allow: fallback (reason: optional dependency not installed)
        raise RuntimeError(
            "runtime: slurm-tenant requires scitex-hpc>=0.5.1. "
            "Install via: pip install 'scitex-agent-container[slurm]'"
        ) from exc
    return Reservation


class SlurmTenantRuntime(RuntimeBase):
    """Run an agent as a tenant of a pre-booked scitex-hpc Reservation."""

    def _tmux_session(self, cfg: AgentConfig) -> str:
        """tmux session name on the compute node — namespaced to avoid collisions."""
        return f"sac-{cfg.name}"

    def _tmux_socket(self, res) -> str:
        """Return the named tmux socket the reservation was booked with.

        Phase 4 architectural fix: tenant tmux sessions must connect to
        the long-lived tmux server bootstrapped by the sbatch hold body
        (scitex-hpc>=0.6.0 with ``Reservation.book(tmux_server=...)``).
        Without it, ``tmux new-session`` runs in the srun step's cgroup
        and gets killed when the step exits.
        """
        socket = (res.extras or {}).get("tmux_server")
        if not socket:
            raise RuntimeError(
                f"reservation {res.id!r} was not booked with tmux_server set. "
                "Re-book via: scitex-hpc reservations book <name> --host <h> "
                "--tmux-server sac ...  (slurm-tenant runtime requires the "
                "server bootstrap; otherwise tmux daemons get cgroup-killed "
                "when srun --overlap steps end.)"
            )
        return socket

    def _tmux(self, res, *args: str) -> str:
        """Compose a ``tmux -L <socket> <args>`` command string."""
        socket = self._tmux_socket(res)
        return "tmux -L " + shlex.quote(socket) + " " + " ".join(args)

    def _resolve_reservation(self, cfg: AgentConfig):
        """Look up the Reservation; raise with a clear message if missing."""
        Reservation = _import_reservation()
        name = cfg.slurm.reservation
        if not name:
            raise RuntimeError(
                "runtime: slurm-tenant requires spec.slurm.reservation to be set "
                "(name of the scitex-hpc lease this agent should join)"
            )
        res = Reservation.get(name)
        if res is None:
            # Try matching by lease id directly
            res = Reservation.get(name)
        if res is None:
            raise RuntimeError(
                f"reservation {name!r} not found. "
                f"Book one first: scitex-hpc reservations book {name} --host <h> ..."
            )
        if not res.job_id:
            res.refresh()
        if not res.job_id:
            raise RuntimeError(
                f"reservation {name!r} exists but has no live job_id. "
                "Has the SLURM job exited? Check 'scitex-hpc reservations get'."
            )
        return res

    def _build_claude_command(self, cfg: AgentConfig) -> str:
        """Render the ``claude`` invocation as a single shell-quotable string."""
        parts = ["claude"]
        if cfg.model:
            parts.extend(["--model", cfg.model])
        for ch in cfg.claude.channels:
            parts.extend(["--channels", ch])
        for flag in cfg.claude.flags:
            parts.append(flag)
        if cfg.claude.session == "continue":
            parts.append("--continue")
        return shlex.join(parts)

    def _exec_on_node(
        self, config: AgentConfig, res, cmd_str: str, timeout: int = 60
    ) -> subprocess.CompletedProcess:
        """Execute ``cmd_str`` on the compute node.

        When ``spec.remote.hops`` is set, SSH directly via the chain
        (location-aware self-skip applied).  Falls back to
        ``res.exec()`` when hops is empty (legacy srun path).
        """
        hops = config.remote.hops
        if not hops:
            return res.exec(cmd_str, check=False, timeout=timeout)

        remaining = skip_local_hops(hops)
        if not remaining:
            # All hops matched local host — run tmux command directly.
            return subprocess.run(
                cmd_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        ssh_cmd = build_ssh_command(remaining, cmd_str, _SSH_OPTS)
        return subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        """Start a tmux session inside the existing reservation."""
        res = self._resolve_reservation(config)
        session = self._tmux_session(config)

        # Force: kill any stale session with the same name first
        if force:
            self._exec_on_node(
                config,
                res,
                self._tmux(res, "kill-session", "-t", shlex.quote(session))
                + " 2>/dev/null || true",
            )

        # Bail if the session already exists and we're not forcing
        if not force:
            check = self._exec_on_node(
                config,
                res,
                self._tmux(res, "has-session", "-t", shlex.quote(session))
                + " 2>/dev/null && echo HAS || echo NONE",
            )
            if "HAS" in (check.stdout or ""):
                logger.info(
                    "SlurmTenantRuntime: %s already has a tmux session in %s",
                    config.name,
                    res.id,
                )
                return True

        claude_cmd = self._build_claude_command(config)
        # tmux new -d (detached) -s <session> '<command>'
        # The double-shell-quote is intentional: outer wrapper for ssh/srun, inner
        # for tmux's command argument.
        tmux_cmd = self._tmux(
            res,
            "new-session",
            "-d",
            "-s",
            shlex.quote(session),
            shlex.quote(claude_cmd),
        )
        result = self._exec_on_node(config, res, tmux_cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux new-session failed in reservation {res.id}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        logger.info(
            "SlurmTenantRuntime: %s started in tmux %s @ reservation %s (job %s, node %s)",
            config.name,
            session,
            res.id,
            res.job_id,
            res.node,
        )
        return True

    def stop(self, config: AgentConfig) -> bool:
        """Kill the agent's tmux session. Does NOT release the reservation."""
        try:
            res = self._resolve_reservation(config)
        except RuntimeError as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            logger.warning("SlurmTenantRuntime: cannot stop %s — %s", config.name, exc)
            return False
        session = self._tmux_session(config)
        result = self._exec_on_node(
            config,
            res,
            self._tmux(res, "kill-session", "-t", shlex.quote(session))
            + " 2>&1 || true",
        )
        logger.info(
            "SlurmTenantRuntime: stopped tmux %s @ reservation %s (rc=%s)",
            session,
            res.id,
            result.returncode,
        )
        return True

    def is_running(self, config: AgentConfig) -> bool:
        try:
            res = self._resolve_reservation(config)
        except RuntimeError:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            return False
        session = self._tmux_session(config)
        result = self._exec_on_node(
            config,
            res,
            self._tmux(res, "has-session", "-t", shlex.quote(session))
            + " 2>/dev/null && echo HAS || echo NONE",
        )
        return "HAS" in (result.stdout or "")

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Capture the last ``lines`` of pane content from the tmux session."""
        try:
            res = self._resolve_reservation(config)
        except RuntimeError as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            return f"(reservation unavailable: {exc})"
        session = self._tmux_session(config)
        # tmux capture-pane writes to stdout with -p
        result = self._exec_on_node(
            config,
            res,
            self._tmux(
                res,
                "capture-pane",
                "-p",
                "-t",
                shlex.quote(session),
                "-S",
                f"-{int(lines)}",
            )
            + " 2>/dev/null || echo '(no session)'",
        )
        return result.stdout or ""

    def attach(self, config: AgentConfig) -> int:
        """Open an interactive tmux attach against the tenant's session.

        Uses ``Reservation.attach`` (which runs ``srun --jobid --pty``)
        to enter the compute node, then ``tmux -L <socket> attach -t
        <session>`` to attach the operator's terminal to the running
        agent. Detach with the standard tmux prefix (Ctrl-B D).
        """
        res = self._resolve_reservation(config)
        session = self._tmux_session(config)
        # Use the reservation's --pty channel; build the inner command as
        # a single shell-quoted string for tmux attach.
        attach_cmd = self._tmux(res, "attach", "-t", shlex.quote(session))
        return res.attach(cmd=attach_cmd, pty=True)


__all__ = ["SlurmTenantRuntime"]
