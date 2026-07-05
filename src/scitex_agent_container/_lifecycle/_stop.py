"""``agent_stop`` / ``agent_stop_all`` / ``agent_restart``.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports all three names.
"""

from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ._a2a_port import release_a2a_port
from ._handover_loader import _load_handover_module
from ._hook_runner import _fire_forget_hook, _run_hooks
from ._instances import end_local_instance as _end_local_instance
from ._runtime_select import _get_runtime

logger = logging.getLogger(__name__)

# Default upper bound on how long ``agent_restart`` will wait for the
# previous runtime to actually exit before starting the new one. Tuned
# for apptainer healthy teardown (~0.5–2 s); 15 s leaves comfortable
# headroom for a loaded host. The race this closes: new container boots
# while the old one still holds /home/agent overlay + per-agent stdio MCP
# child still holds its PID lock file → new bun child's ``acquireLock``
# sees the live old PID and exits 1 → claude silently drops the MCP.
_DEFAULT_WAIT_FOR_STOP_TIMEOUT_S = 15.0
# Poll interval while waiting. 0.25 s is small enough that a 1-second
# teardown costs <=4 polls; large enough that a healthy host doesn't
# spend measurable CPU on the loop. ``sleep_fn`` controls real-time
# behavior; tests pass ``_no_sleep`` to spin as fast as Python allows.
_WAIT_FOR_STOP_POLL_INTERVAL_S = 0.25


def agent_stop(
    name: str,
    registry: Registry | None = None,
    force: bool = False,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    handover_mod: Any = None,
) -> bool:
    """Stop a running agent by name.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        force: If True, do not fail when the agent is missing from the
            registry or when hooks/runtime.stop() raise; wipe stale
            state and return True. Useful for bulk cleanup.
        runtime_factory: Injectable real runtime factory (default
            :func:`_get_runtime`).
        handover_mod: Injectable real handover collaborator (default
            ``None`` resolves to the real
            :mod:`._lifecycle.handover` module).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        if force:
            return True
        raise RuntimeError(f"Agent '{name}' not found in registry")

    # stx-allow: fallback (reason: YAML file may have been deleted while the agent was registered; force-stop must succeed even without a config)
    try:
        config = load_config(entry["config"])
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
        # Config gone — just nuke the registry entry
        registry.remove(name)
        return True

    runtime_factory = runtime_factory or _get_runtime
    runtime = runtime_factory(config)

    hook_env = {
        "SCITEX_AGENT_CONTAINER_CONFIG_PATH": str(Path(entry["config"]).resolve()),
        "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
        "SCITEX_AGENT_CONTAINER_NAME": config.name,
    }

    # ZOO#12 FR-A — push a sentinel snapshot to the hub right before
    # the agent stops, so a future agent_start (here or on a different
    # host) can hydrate. Best-effort: never block the stop path on a
    # hub outage. The sentinel is a marker; the agent's own pre_stop
    # hook is the right place for richer state (transcript, memory).
    try:
        _h = handover_mod if handover_mod is not None else _load_handover_module()
        _h.push_pre_stop_snapshot(config)
    except Exception:
        traceback.print_exc()

    # Fleet-default pre-stop rescue (operator priority, lead a2a
    # efa48850daf248ed9fe3ae5232677b2b). Commits + pushes every dirty
    # worktree (or diff-tarballs them on protected/push-failure) before
    # the agent dies, so restart never silently loses uncommitted work.
    # NEVER raises — bounded by RESCUE_GRACE_SECONDS; whatever finished
    # before the budget elapsed is preserved.
    from ._pre_stop_rescue import run_pre_stop_rescue

    run_pre_stop_rescue(config)

    # Pre-stop hooks
    # stx-allow: fallback (reason: hook commands may reference paths or env vars absent at stop time; force-stop must continue regardless)
    try:
        _run_hooks(config.hooks.get("pre_stop", []), extra_env=hook_env)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
    _fire_forget_hook(config.name, "pre_stop", config.hooks.get("pre_stop", []))

    # stx-allow: fallback (reason: tmux/screen session may already be dead; force-stop should still proceed to clean up registry)
    try:
        runtime.stop(config)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise

    # Post-stop hooks
    # stx-allow: fallback (reason: post-stop hooks are best-effort notification; a failed hook must not prevent registry cleanup)
    try:
        _run_hooks(config.hooks.get("post_stop", []), extra_env=hook_env)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not force:
            raise
    _fire_forget_hook(config.name, "post_stop", config.hooks.get("post_stop", []))

    # Mark the local state.db ``instances`` row ended so subsequent
    # ``send_to_agent`` calls correctly report "not running" and the
    # unique (name, host, scope) active-row index is freed for a restart.
    _end_local_instance(config, runtime)

    # Release the A2A port claim so the next agent can re-use it.
    release_a2a_port(name)
    registry.remove(name)
    return True


def agent_stop_all(
    registry: Registry | None = None,
    force: bool = False,
    *,
    stop_fn: Optional[Callable[..., bool]] = None,
) -> list[tuple[str, bool, str]]:
    """Stop every agent in the registry.

    Returns a list of ``(name, success, message)`` tuples, one per agent.
    With ``force=True``, continues through errors so a partial failure
    doesn't block cleanup of the rest.

    Args:
        registry: Optional registry instance.
        force: Continue through individual-agent failures.
        stop_fn: Injectable real per-agent stop callable (default
            ``None`` uses module-level :func:`agent_stop`). Tests pass a
            real callable that records calls and optionally raises.
    """
    registry = registry or Registry()
    stopper = stop_fn or agent_stop
    results: list[tuple[str, bool, str]] = []
    for entry in registry.list_all():
        name = entry.get("name", "?")
        # stx-allow: fallback (reason: stopping one agent may fail due to a missing config or dead session; other agents in the registry should still be stopped)
        try:
            stopper(name, registry=registry, force=force)
            results.append((name, True, "stopped"))
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            results.append((name, False, str(exc)))
            if not force:
                break
    return results


def _wait_for_previous_runtime_to_exit(
    name: str,
    config_path: str,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]],
    sleep_fn: Callable[[float], None],
    timeout_s: float,
) -> bool:
    """Block until the previous runtime instance is no longer running.

    Returns True if the runtime stopped cleanly within ``timeout_s``,
    False on timeout (caller proceeds anyway after a LOUD warning — see
    ``agent_restart`` docstring for the race this closes). ``timeout_s
    <= 0`` skips the gate entirely (legacy ``sleep_fn(2)`` behaviour,
    preserved for callers that want the bypass).
    """
    if timeout_s <= 0:
        sleep_fn(2)
        return True
    # Load once: config is stable across the gate and re-loading per poll
    # would multiply YAML parsing on a busy host.
    try:
        config = load_config(config_path)
    except Exception:  # stx-allow: fallback (reason: YAML may have been edited mid-restart; fall back to the legacy fixed sleep instead of blocking the restart on a transient parse error — the new container will surface the real error when it boots)
        sleep_fn(2)
        return True
    factory = runtime_factory or _get_runtime
    runtime = factory(config)
    deadline = time.monotonic() + timeout_s
    while runtime.is_running(config):
        if time.monotonic() >= deadline:
            logger.warning(
                "agent_restart for %r: previous runtime still running after "
                "%.2fs (SIGTERM ignored or teardown hung); proceeding to "
                "start anyway. WARNING: this may trigger the apptainer "
                'double-mount race ("destination is already in the mount '
                'point list") and stdio-MCP lock-file contention (the '
                "standalone bun MCP poller exits 1 on lock held by "
                "the orphaned previous PID, claude then silently drops the "
                "MCP). If %r repeatedly fails to honor SIGTERM, investigate "
                "the runtime stop path (see ApptainerContainerRuntime.stop).",
                name,
                timeout_s,
                name,
            )
            return False
        sleep_fn(_WAIT_FOR_STOP_POLL_INTERVAL_S)
    return True


def agent_restart(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    handover_mod: Any = None,
    config_resolver: Optional[Callable[[str], str]] = None,
    wait_for_stop_timeout_s: float = _DEFAULT_WAIT_FOR_STOP_TIMEOUT_S,
) -> bool:
    """Restart an agent by name: resolve spec → stop → settle → start.

    The spec path is resolved with this precedence:

      1. The registry ``instances``/registry row for ``name`` (recorded
         by a Phase-1-era ``agent_start``), then
      2. the agent's spec, found via ``config_resolver`` (default
         :func:`config.resolve_config`) walking the standard discovery
         chain.

    The spec fallback is the robustness path for **ad-hoc-launched**
    agents — agents started by a bare runner invocation rather than
    ``sac agents start`` (so they predate the auto-record and have no
    registry row). Without it, ``restart`` hard-failed with
    "not found in registry" for exactly those agents (the Spartan
    compute-node case, 2026-05-24). The stop leg uses ``force=True`` so
    a missing/stale registry row never blocks the kill — it mirrors the
    working manual recipe (``stop --yes`` then ``start --yes``).

    Cross-host routing is the **CLI**'s responsibility
    (``cli_pkg/lifecycle/_restart.py`` dispatches to the agent's
    recorded host before reaching here, like ``stop`` does). By the
    time control reaches this function the target is local.

    Teardown gate (2026-06-07, bug #42 — channel MCP drops after restart):
    Between ``agent_stop`` (which sends SIGTERM and returns immediately —
    ``ApptainerContainerRuntime.stop`` says "No wait loop yet — sac's
    outer lifecycle does its own poll") and ``agent_start``, this function
    now polls ``runtime.is_running`` until False or
    ``wait_for_stop_timeout_s`` elapses. The legacy fixed ``sleep_fn(2)``
    was the SOLE settle window — it raced the apptainer teardown on a
    loaded host. Operator-visible symptom: the new container booted while
    the old one still held the ``/home/agent`` overlay (apptainer warned
    "destination is already in the mount point list"), AND the old SDK's
    per-agent stdio MCP child (a standalone bun MCP poller)
    was still alive holding its per-agent lock file.
    The new bun child's ``acquireLock`` then hit ``process.kill(old_pid,
    0)`` SUCCESS → ``process.exit(1)``, claude silently marked the MCP
    failed and never retried it. ``sac`` + Mermaid MCPs reloaded fine
    (no inter-instance lock for those), masking the bug as "only Telegram
    broke." On timeout we LOG LOUD (so a stuck previous instance is
    self-diagnosing from stdout.log) and proceed — silently spinning
    forever locks the operator out of restart entirely.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
        sleep_fn: Real sleep (default ``time.sleep``).
        handover_mod: Real handover collaborator (default ``None``
            resolves to the real module).
        config_resolver: Real name→spec-path resolver (default
            :func:`config.resolve_config`). Injected for tests so the
            no-registry-row fallback can be exercised against a real
            on-disk spec without monkeypatching internals.
        wait_for_stop_timeout_s: Upper bound on the previous-runtime
            readiness gate (see "Teardown gate" above). Default 15 s —
            ~10× a healthy apptainer teardown. Set to 0 to skip the gate
            entirely (legacy behaviour, retained for tests of unrelated
            code paths).

    Raises:
        RuntimeError: When ``name`` has neither a registry row NOR a
            resolvable spec — a genuinely unknown agent.
    """
    # Lazy import breaks the ``_start`` <-> ``_stop`` cycle.
    from ._start import agent_start

    registry = registry or Registry()
    entry = registry.get(name)

    if entry is not None:
        config_path = entry["config"]
    else:
        # No registry row (ad-hoc / pre-autorecord launch). Resolve the
        # spec from the standard discovery chain rather than hard-failing.
        resolver = config_resolver
        if resolver is None:
            from ..config import resolve_config as resolver
        # stx-allow: fallback (reason: translate a FileNotFoundError from the
        # resolver into a single clear "neither registry row nor spec" error;
        # both lookups genuinely failed, so this is fail-loud, not a silent
        # default-substitution)
        try:
            config_path = resolver(name)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Agent '{name}' not found in registry and no spec could be "
                f"resolved by name ({exc}). Pass a spec path, or start the "
                f"agent once via 'sac agents start' so a registry row exists."
            ) from exc

    # force=True so a missing/stale registry row never blocks the kill —
    # this is what makes restart == the manual stop+start recipe even for
    # ad-hoc-launched agents with no row.
    agent_stop(
        name,
        registry,
        force=True,
        runtime_factory=runtime_factory,
        handover_mod=handover_mod,
    )
    _wait_for_previous_runtime_to_exit(
        name,
        config_path,
        runtime_factory=runtime_factory,
        sleep_fn=sleep_fn,
        timeout_s=wait_for_stop_timeout_s,
    )
    # Clear BOTH the persisted session_id AND session_id_history before the
    # restart. A plain ``agent_restart`` previously called ``agent_start``
    # WITHOUT force=True (so no session reset ran at all), and the
    # ``--force`` path itself only cleared ``session_id`` — leaving a dead
    # uuid in the append-only history that the runner's resume fallback
    # RE-RESUMED and RE-CRASHED. That is why ``sac agents restart`` could
    # not recover a DEAD session (clew/neurovista, 2026-05-24): the manual
    # recovery had to clear both and back them up. Doing it here makes a
    # plain restart self-recovering regardless of the start path's force
    # flag. ``_clear_persisted_session_id`` backs both up to
    # ``session_id_history.dead-<ts>`` and is a no-op on a clean state dir.
    from ._session_reset import _clear_persisted_session_id

    _clear_persisted_session_id(name)
    return agent_start(
        config_path,
        registry,
        runtime_factory=runtime_factory,
        sleep_fn=sleep_fn,
        handover_mod=handover_mod,
    )
