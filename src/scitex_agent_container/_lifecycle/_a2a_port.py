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

# Marks an ``A2ASpec`` whose int ``port`` WE auto-allocated, rather than one the
# operator pinned. Set on the spec OBJECT (not persisted) because the two are
# otherwise indistinguishable after the mutation below, and they are NOT the
# same request — see ``claim_port(explicit_is_pin=...)``.
_AUTO_ORIGIN = "_sac_port_auto_allocated"


def resolve_a2a_port(config: AgentConfig) -> None:
    a2a = getattr(config, "a2a", None)
    if a2a is None:
        return
    raw = getattr(a2a, "port", None)
    if raw is None:
        return  # sidecar disabled — no claim needed
    if isinstance(raw, str) and raw == "auto":
        a2a.port = port_allocator.claim_port(config.name)
        # REMEMBER THE ORIGIN. The line above MUTATES "auto" -> an int, so by
        # the next call this spec is indistinguishable from an operator pin.
        # `agent_start`'s force/restart path calls us AGAIN after `agent_stop`
        # released the claim — so without this marker, restarting an ordinary
        # auto-port agent silently re-entered the PINNED-port code, and a lost
        # race there raised instead of just taking another free port. That is
        # the path the v0.21.19 release failure ran down.
        setattr(a2a, _AUTO_ORIGIN, True)
        return
    if isinstance(raw, int) and raw > 0:
        # Record the claim so the cross-process routing layer (sac listen) finds
        # the agent in state.db. If the operator changed the pin between starts,
        # claim_port updates the row to the new value (releasing the old).
        #
        # `explicit_is_pin` decides what a LOST RACE means: a genuine operator
        # pin held by someone else is a misconfiguration and must raise; a port
        # we merely auto-allocated before this restart is a preference, so a
        # fresh one is correct and failing the launch is not.
        a2a.port = port_allocator.claim_port(
            config.name,
            explicit=raw,
            explicit_is_pin=not getattr(a2a, _AUTO_ORIGIN, False),
        )


def release_a2a_port(name: str) -> None:
    """Drop the claim for ``name``. Called by ``agent_stop``."""
    port_allocator.release_port(name)
