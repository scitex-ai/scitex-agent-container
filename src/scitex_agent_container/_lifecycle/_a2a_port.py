"""Resolve ``config.a2a.port`` at agent_start time.

Three input states (see :class:`A2ASpec`):

  * ``"auto"`` — claim a free port from the allocator's range and write
    it back onto ``config.a2a.port`` so the runtime argv builder picks
    up an int.
  * ``int``    — operator-pinned. Recorded in the claim table so ``sac
    listen`` / ``sac agents list`` can look the agent up without
    re-parsing the spec.
  * ``None``   — sidecar explicitly disabled; nothing to do.

Mutates ``config.a2a`` in-place. Safe to call multiple times for the
same agent (claim_port is idempotent on agent_name).
"""

from __future__ import annotations

from .._state import port_allocator
from ..config import AgentConfig


def resolve_a2a_port(config: AgentConfig) -> None:
    a2a = getattr(config, "a2a", None)
    if a2a is None:
        return
    raw = getattr(a2a, "port", None)
    if raw is None:
        return  # sidecar disabled — no claim needed
    if isinstance(raw, str) and raw == "auto":
        a2a.port = port_allocator.claim_port(config.name)
        return
    if isinstance(raw, int) and raw > 0:
        # Operator-pinned: record the claim so the cross-process
        # routing layer (sac listen) finds the agent in state.db. If
        # the operator changed the pin between starts, claim_port
        # updates the row to the new value (releasing the old).
        a2a.port = port_allocator.claim_port(config.name, explicit=raw)


def release_a2a_port(name: str) -> None:
    """Drop the claim for ``name``. Called by ``agent_stop``."""
    port_allocator.release_port(name)
