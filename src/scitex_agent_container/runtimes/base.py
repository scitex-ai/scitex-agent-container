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
    ) -> bool:
        """Start an agent. Returns True on success.

        ``force=True`` instructs the runtime to stop any existing
        instance before starting, and (for dispatchers like SSHRemote)
        to relay ``--force`` to the downstream CLI.
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
