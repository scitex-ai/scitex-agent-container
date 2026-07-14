"""Abstract base class for agent runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import AgentConfig


class RuntimeBase(ABC):
    """Interface that all runtime adapters must implement."""

    @abstractmethod
    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        """Start an agent. Returns True on success.

        ``force=True`` instructs the runtime to stop any existing
        instance before starting, and (for dispatchers like SSHRemote)
        to relay ``--force`` to the downstream CLI.

        ``dry_run=True`` instructs the runtime to materialize the
        workspace (CLAUDE.md, .mcp.json, .env, settings.json) but to
        skip launching the multiplexer / agent process. Runtimes that
        cannot meaningfully dry-run should raise ``NotImplementedError``.

        ``foreground=True`` instructs the runtime to keep the agent
        attached to the caller's terminal (no detach, stdio inherited)
        and block until the agent exits. Runtimes that have no
        meaningful foreground mode (e.g. screen / tmux runtimes whose
        whole point is detachment) ignore this flag.
        """
        ...

    @abstractmethod
    def stop(self, config: AgentConfig) -> bool:
        """Stop a running agent. Returns True on success."""
        ...

    @abstractmethod
    def is_running(self, config: AgentConfig) -> bool:
        """Check if the agent is currently running."""
        ...

    @abstractmethod
    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Retrieve recent output from the agent."""
        ...

    def agent_pid(self, config: AgentConfig) -> int | None:
        """Return the LONG-LIVED OS pid backing the agent, or ``None``.

        This is the pid ``instances.pid`` records (see
        :func:`_lifecycle._instances.record_local_instance`), and the
        contract is deliberately narrow: return the SAME pid this
        runtime's own :meth:`is_running` keys its liveness verdict on —
        the process that stays alive for the whole session — never the
        launcher.

        The distinction is load-bearing. A launcher pid is WRONG here: a
        TUI agent's launcher spawns a tmux session and exits within
        seconds, so recording it would store a pid that is dead almost
        immediately, and every consumer probing it (``os.kill(pid, 0)``
        in :func:`_state.state_db_gc.gc_dead_instances`,
        :func:`_lifecycle._stale_lease.clear_stale_instance_lease`,
        :func:`cli_pkg._send_diagnosis.diagnose_send_failure`) would
        report a LIVE agent as dead.

        NOT abstract, and the default is ``None`` ON PURPOSE. A runtime
        that cannot name a long-lived local pid (docker / podman /
        SSHRemote — the process lives in another namespace or on another
        host) MUST leave this ``None``. ``None`` is honestly "unknown"
        and every consumer treats it as such; a plausible-but-wrong pid
        is strictly worse, because pids are REUSED — a stale one can be
        recycled by an unrelated process and would then vouch for a dead
        agent as alive.
        """
        del config
        return None
