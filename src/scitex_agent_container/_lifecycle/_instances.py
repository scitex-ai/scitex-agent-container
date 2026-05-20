"""state.db ``instances`` row bookkeeping for LOCAL agent starts/stops.

The cross-host dispatcher (``cli_pkg/lifecycle/_dispatch.py``) writes the
lead-side ``instances`` row for agents it launches on a remote peer. A
LOCAL ``sac agent start`` had no equivalent: the row was never created,
so ``send_to_agent`` / the MCP ``agent_send`` tool reported "agent not
running" and the bound ``/v1/turn`` sidecar was unreachable.

These helpers close that gap. ``record_local_instance`` is called by
:func:`._lifecycle.lifecycle.agent_start` after a successful local
``runtime.start``; ``end_local_instance`` by ``agent_stop``. The
resolved a2a_port comes from the port allocator (set by
``resolve_a2a_port`` during ``agent_start``) so ``/v1/turn`` routing has
the port it needs.

The instance uuid is persisted under the runtime state dir
(``<state_dir>/instance_id``) so the stop path resolves the exact row
without rescanning by name+host.
"""

from __future__ import annotations

from typing import Any

from ..config import AgentConfig


def _state_dir_for(config: AgentConfig, runtime: Any):
    """Per-agent runtime state dir, via the runtime's own resolver.

    Both ApptainerContainerRuntime and the claude-session runtime expose
    ``_state_dir``; fall back to the runner default when a runtime stub
    in a test doesn't. Returns ``None`` only when no resolver exists.
    """
    resolver = getattr(runtime, "_state_dir", None)
    if callable(resolver):
        return resolver(config)
    from .._runners import claude_session as _runner

    return _runner.state_dir_for(config.name)


def record_local_instance(config: AgentConfig, runtime: Any) -> str | None:
    """Insert (or refresh) the local ``instances`` row for ``config``.

    Ends any stale active row for the same (name, host) first so the
    unique partial index ``instances(name, host, scope) WHERE ended_at
    IS NULL`` never collides on a restart. Returns the new instance id,
    or ``None`` when the runtime can't report a state dir.
    """
    from .._runners._session_state import write_instance_id
    from .._state.port_allocator import get_port
    from .._state.state_db import (
        _resolve_host,
        list_active_instances,
        record_instance_start,
        record_instance_stop,
    )

    host = _resolve_host(None)
    # End stale active rows for this name+host (e.g. a previous crash
    # that never reached agent_stop) so the unique index stays clear.
    for row in list_active_instances(host=host):
        if row.get("name") == config.name:
            record_instance_stop(str(row["id"]), exit_reason="superseded")

    a2a_port = get_port(config.name)
    workdir = getattr(config, "expanded_workdir", None) or getattr(
        config, "workdir", None
    )
    instance_id = record_instance_start(
        name=config.name,
        host=host,
        a2a_port=a2a_port,
        workdir=str(workdir) if workdir else None,
    )

    state_dir = _state_dir_for(config, runtime)
    if state_dir is not None:
        write_instance_id(state_dir, instance_id)
    return instance_id


def end_local_instance(config: AgentConfig, runtime: Any) -> bool:
    """Mark the local ``instances`` row for ``config`` ended.

    Resolves the row id from the persisted ``<state_dir>/instance_id``
    marker when present, else falls back to the active row matching
    name+host. Returns True iff a row was updated.
    """
    from .._runners._session_state import clear_instance_id, read_instance_id
    from .._state.state_db import (
        _resolve_host,
        list_active_instances,
        record_instance_stop,
    )

    state_dir = _state_dir_for(config, runtime)
    instance_id: str | None = None
    if state_dir is not None:
        instance_id = read_instance_id(state_dir)

    updated = False
    if instance_id:
        updated = record_instance_stop(instance_id, exit_reason="stopped")
    if not updated:
        host = _resolve_host(None)
        for row in list_active_instances(host=host):
            if row.get("name") == config.name:
                updated = record_instance_stop(str(row["id"]), exit_reason="stopped")
                break

    if state_dir is not None:
        clear_instance_id(state_dir)
    return updated
