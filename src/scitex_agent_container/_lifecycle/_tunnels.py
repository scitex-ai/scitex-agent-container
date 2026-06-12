"""Process-local registry of active :class:`TunnelManager` instances.

The lifecycle wiring needs to share a TunnelManager between
:func:`agent_start` (which calls ``mgr.up()`` and stores the bound
port for the runtime to inject as ``ANTHROPIC_BASE_URL``) and
:func:`agent_stop` (which calls ``mgr.down()`` to tear the supervisor
down). The state.db ``instances`` row is the canonical cross-process
"is this agent alive" record, but the TunnelManager Python object
itself can't be persisted there — the supervisor pid IS persisted by
the manager via its own pidfile, but the manager's in-memory
``subprocess.Popen`` handle is process-local.

This module owns the process-local registry. It's tiny, but lives in
its own file so the import surface is explicit and tests can clear
state without poking at private module state.

Cross-process recovery (agent stops in a different sac CLI invocation
than the one that started it) is handled by the pidfile path on disk:
:class:`TunnelManager.down` reads the pidfile when no Popen handle is
available, signals the supervisor, and removes the pidfile. The
in-memory registry is just an optimization for same-process flows.
"""

from __future__ import annotations

from typing import Dict, Optional

from .._network._tunnel_manager import TunnelManager

# Module-level dict keyed by agent name; one tunnel per agent at a time.
_ACTIVE: Dict[str, TunnelManager] = {}


def register(name: str, manager: TunnelManager) -> None:
    """Record an active tunnel manager so :func:`get` can recover it."""
    _ACTIVE[name] = manager


def get(name: str) -> Optional[TunnelManager]:
    """Return the active manager for ``name``, or ``None`` if not registered."""
    return _ACTIVE.get(name)


def discard(name: str) -> None:
    """Drop the registry entry for ``name``. Idempotent — missing is OK."""
    _ACTIVE.pop(name, None)


def clear() -> None:
    """Wipe the entire registry. Used by tests to isolate cases."""
    _ACTIVE.clear()


__all__ = ["register", "get", "discard", "clear"]
