"""Gateway-harness shared types — the import-cycle-free leaf.

``runtimes/_gateway_harness.py`` (contract + Hermes adapter) and
``runtimes/_gateway_opencode.py`` (driver #2) both need the error and
the protocol, and the contract module registers the driver at import
time. If either lived in the contract module, whichever of the two
was imported first would hit a partially-initialized module. This
leaf imports nothing from the package, so the cycle cannot form.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["GatewayHarness", "GatewayHarnessError"]


class GatewayHarnessError(RuntimeError):
    """A gateway-harness operation cannot be honoured.

    Raised fail-loud (naming the harness and the remedy) instead of
    silently degrading to terminal emulation or another harness.
    """


@runtime_checkable
class GatewayHarness(Protocol):
    """The operations every server+TUI harness implements."""

    @property
    def name(self) -> str:
        """Registry identity (``hermes-tui``)."""
        ...

    @property
    def spec_key(self) -> str:
        """``available_harnesses`` key (``hermes``)."""
        ...

    def owner_argv(self, config: Any, state_dir: Path) -> list[str]:
        """Inner argv: loopback server as owner + TUI attached."""
        ...

    def session_states(
        self, state_dir: Path, *, timeout_s: float = 10.0
    ) -> dict[str, str]:
        """Live session id -> ``busy`` | ``idle`` from the server."""
        ...

    def submit_turn(
        self,
        state_dir: Path,
        agent_name: str,
        text: str,
        *,
        delivery_mode: str = "steer",
        timeout_s: float = 10.0,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Deliver one turn; ``steer`` (default) or ``queue`` when busy.

        ``message_id`` carries the caller-side idempotency key when the
        harness supports client-set ids (opencode); harnesses that own
        idempotency at the bridge (Hermes) ignore it.
        """
        ...

    def abort_turn(
        self, state_dir: Path, agent_name: str, *, timeout_s: float = 10.0
    ) -> bool:
        """Cancel the in-flight run; True when the server confirmed."""
        ...

    def parse_agent_options(self, config: Any) -> dict[str, Any]:
        """Per-agent tunables, read off the loaded spec-derived config."""
        ...

    def compile_profile(
        self, plan: Any, *, workdir: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        """Credential-free derived server profile for one incarnation."""
        ...
