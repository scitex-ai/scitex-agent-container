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


def _spawned_by() -> str:
    """Return the launching identity for the lineage edge (Rule B/D).

    When a PARENT agent shells out ``sac agents start <child>``, its
    container env carries ``SAC_NAME`` (the parent's own name), so the
    child's row records ``spawned_by=<parent>``. A bare CLI / lead /
    operator launch has no ``SAC_NAME`` and records ``"cli"``. Richer
    attribution (operator vs lead vs a specific human) is a documented
    follow-on phase; the schema column is ready for it now.
    """
    from .._env import getenv

    return getenv("NAME") or "cli"


def _runtime_pid(config: AgentConfig, runtime: Any) -> int | None:
    """Resolve the LONG-LIVED pid of the process the runtime just started.

    Asks the runtime itself (:meth:`runtimes.base.RuntimeBase.agent_pid`)
    rather than guessing, so the pid landing in ``instances.pid`` is the
    SAME one that runtime's own ``is_running`` probes with
    ``os.kill(pid, 0)``:

      * TUI (the default runtime)  -> the tmux PANE pid, which IS the
        long-lived ``apptainer exec ... claude`` process (the pane's
        ``bash -c`` ``exec``s apptainer, and ``exec`` keeps the pid).
      * SDK / apptainer            -> the ``apptainer`` process pid that
        ``ApptainerRuntime.start`` persisted to
        ``<state_dir>/apptainer_pid``.

    NOT the launching process: for a TUI agent the launcher spawns the
    tmux session and EXITS within seconds, so recording it would store a
    pid that is dead almost immediately — reproducing the very bug this
    fixes, while looking like a fix.

    Returns ``None`` (honestly "unknown") when the runtime cannot name a
    pid — an older/injected runtime without the seam, a docker/podman
    container, or a probe that failed. ``None`` is SAFE by construction:
    every consumer treats a NULL pid as "no verdict"
    (:func:`_state.state_db_gc.gc_dead_instances` skips it,
    :func:`_lifecycle._stale_lease.clear_stale_instance_lease` leaves the
    row alone, :func:`cli_pkg._send_diagnosis._pid_alive` returns
    ``None``), whereas a WRONG pid is strictly worse — pids get REUSED,
    so a stale one can be recycled by an unrelated process and would then
    vouch for a dead agent as alive.
    """
    getter = getattr(runtime, "agent_pid", None)
    if not callable(getter):
        return None
    try:
        pid = getter(config)
    except Exception:  # stx-allow: fallback (reason: a pid probe hiccup must never block an agent start; NULL is the honest "unknown" and is safe for every consumer — see docstring)
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


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
    # Rule B (sac-agent-spawn design): recording the lineage + bound
    # port is an intrinsic side-effect of the start codepath, never a
    # caller responsibility. ``spawned_by`` is the launching identity:
    # the parent agent's SAC_NAME when a parent shelled out, else "cli"
    # for a bare lead/operator launch. ``remote=False`` — this helper is
    # only reached on the host where the agent actually runs; the
    # cross-host dispatcher records the remote=True lead-side row.
    #
    # ``pid`` is the agent's LONG-LIVED process, asked of the runtime that
    # just started it (see :func:`_runtime_pid`). This is the ONLY one of
    # ``record_instance_start``'s call sites that can supply a meaningful
    # pid: the other three write cross-host (``remote=True``) lead-side
    # rows for agents running on a PEER, where a local pid number would be
    # meaningless and dangerous (consumers probe pids with a LOCAL
    # ``os.kill``, so a peer's pid could collide with an unrelated local
    # process and vouch for a dead agent as alive). Those correctly stay
    # NULL.
    instance_id = record_instance_start(
        name=config.name,
        host=host,
        pid=_runtime_pid(config, runtime),
        a2a_port=a2a_port,
        bound_port=a2a_port,
        remote=False,
        spawned_by=_spawned_by(),
        workdir=str(workdir) if workdir else None,
    )

    # ADR-0014 Stage 1 — paired comms_nodes write so cross-host peers
    # can resolve this agent after a `sac registry sync`. The instances
    # table is local; comms_nodes is the federated layer. Best-effort:
    # any error here is logged but does not abort the agent start (a
    # missing comms_nodes row degrades to "peers can't see this agent
    # via the federated graph until next sync" — not a startup blocker).
    if a2a_port is not None:
        try:
            from .._state.state_db_nodes import register_comms_node

            register_comms_node(
                name=config.name,
                host=host,
                a2a_port=int(a2a_port),
                source_host=None,
                # PR L1 (operator directive 12847) — discriminator
                # for the loud-collision error message. ``spec``
                # claims the spec-driven origin; ``source_path`` is
                # the agent's spec-resolved file (best-effort —
                # config.config_path may be None for synthesised
                # configs from tests).
                kind="spec",
                source_path=getattr(config, "config_path", None)
                or getattr(config, "spec_path", None)
                or f"<spec:{config.name}>",
            )
        except Exception:  # stx-allow: fallback (reason: never block agent start on registry write; PR L1's CommsNodeConflictError surfaces here as a logged collision rather than a silent shadow.)
            pass

    # OP-PRIO-1 (split from #343) — refresh the ACL grant ``<self> →
    # lead`` on EVERY successful start. Without this, a previous
    # container that died outside agent_stop (kernel OOM, host reboot,
    # kill -9) leaves the grant either absent (fresh state.db) or
    # untouched-but-correct; the recurrence forced operators to run
    # ``sac a2a grant <agent> lead`` by hand after each restart.
    # ``grant_send`` is idempotent (re-granting the same pair is a
    # no-op, no timestamp bump), so repeat starts do not duplicate
    # the row.
    try:
        from .._state.state_db_nodes import grant_send

        grant_send(
            sender=config.name,
            target="lead",
            note="auto-grant on agent_start (op-2026-06-09)",
        )
    except Exception:  # stx-allow: fallback (reason: never block agent start on grant write; missing grant degrades to operator running `sac a2a grant <name> lead` manually until next start)
        pass

    state_dir = _state_dir_for(config, runtime)
    if state_dir is not None:
        write_instance_id(state_dir, instance_id)
    return instance_id


def restart_and_record(config: AgentConfig, runtime_factory: Any) -> bool:
    """Restart ``config`` via its runtime AND refresh its ``instances`` row.

    The health-monitor's restart callback (wired in :mod:`._start`) used to
    call ``runtime.start(config)`` DIRECTLY, never re-running
    :func:`agent_start` — so a supervisor-restarted agent came back as a
    BRAND-NEW process while its ``instances`` row kept pointing at the old
    one (the split-brain documented in
    :mod:`cli_pkg._send_resolve`).

    That was survivable while ``pid`` was always NULL. It is NOT survivable
    now that the row carries a real pid: the restarted agent's old pid is
    GONE, so ``os.kill(old_pid, 0)`` fails and every consumer
    (:func:`cli_pkg._send_diagnosis`, :func:`_state.state_db_gc`) would
    declare a perfectly LIVE agent dead — ``agent_send`` would refuse with
    "recorded pid is not alive". A stale pid is worse than no pid, so the
    restart path MUST re-record.

    Re-recording (rather than patching the pid in place) is also the
    honest lifecycle model: a restart IS a new instance.
    :func:`record_local_instance` supersedes the previous row and inserts a
    fresh one carrying the new pid + the still-held port claim — exactly
    what ``agent_start`` does.

    Returns whatever ``runtime.start`` returned; the row is refreshed only
    on a successful start (a failed restart must not fabricate a live row).
    """
    runtime = runtime_factory(config)
    started = runtime.start(config)
    if started:
        record_local_instance(config, runtime)
    return started


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

    # ADR-0014 Stage 1 — paired tombstone in comms_nodes. Best-effort.
    if updated:
        try:
            from .._state.state_db_nodes import unregister_comms_node

            unregister_comms_node(name=config.name)
        except (
            Exception
        ):  # stx-allow: fallback (reason: never block agent stop on registry write)
            pass

    if state_dir is not None:
        clear_instance_id(state_dir)
    return updated
