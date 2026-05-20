"""Agent lifecycle management -- start, stop, restart, status.

Public injection seams (real-callable defaults):
    * ``runtime_factory`` — callable taking ``AgentConfig`` and returning a
      runtime object. Default: :func:`_get_runtime`. Used by every
      lifecycle entry point so callers (and tests) can substitute a real
      hand-rolled runtime collaborator without monkeypatching internals.
    * ``sleep_fn`` — callable matching ``time.sleep``. Default ``time.sleep``.
    * ``thread_factory`` — callable matching ``threading.Thread``. Default
      ``threading.Thread``. Used by :func:`agent_start` to spin up the
      health monitor thread.
    * ``handover_mod`` — module-like object exposing ``ensure_instance_uuid``,
      ``hydrate_from_hub``, ``push_pre_stop_snapshot``,
      ``start_failback_poller``. Default ``None`` — the real
      ``_lifecycle.handover`` module is imported lazily.
    * ``runner`` (``_run_hooks``) — callable matching ``subprocess.run``.

All defaults are real production callables, so the public API is unchanged
for existing callers; the seams are documented and tested rather than
hidden behind monkeypatch.
"""

from __future__ import annotations

import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config, resolve_config
from ..hooks import run_hook
from ._a2a_port import release_a2a_port, resolve_a2a_port
from .health import health_monitor


def _get_runtime(config: AgentConfig):
    """Return the SDK runtime for the config.

    Sac is apptainer-only since the 2026-05-13 ripout. Empty / unset
    ``spec.runtime`` is treated as ``"apptainer"``.
    """
    if config.runtime in ("", "apptainer"):
        from ..runtimes.claude_session import ClaudeSessionRuntime

        return ClaudeSessionRuntime()
    raise ValueError(
        f"Unsupported runtime: {config.runtime!r}. "
        "Sac is apptainer-only since 2026-05-13."
    )


def _fallback_workdir(name: str) -> str:
    """Return the workdir path used when the agent's YAML can't be loaded.

    Lands under sac's own user-state dir
    (``~/.scitex/agent-container/runtime/workspaces/<name>/``) per the
    local-state-directories spec — sac never writes to another package's tree.
    """
    return str(
        Path.home() / ".scitex" / "agent-container" / "runtime" / "agents" / name
    )


def _clear_persisted_session_id(name: str) -> None:
    """Wipe ``<runtime-root>/<name>/session_id`` if present.

    Called from :func:`agent_start` on the ``--force`` path so a stale
    SDK resume marker can't ambush the next launch (see the module-level
    explanation at the call site).

    The runtime root is re-read from ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
    every invocation rather than reused from the module-level constant
    captured at import time, so tests (and callers that flip the env
    var per-process) see the fresh value without monkeypatching.

    Loud-by-design: prints a ``[force]`` notice when a file was actually
    removed. Missing file is a no-op (first-ever start). Any other OS
    error is allowed to propagate — the operator wants to know about a
    busted runtime dir, not have it silently swallowed.
    """
    import os
    import sys

    from .._runners._session_state import clear_session_id

    runtime_root = Path(
        os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
            str(Path.home() / ".scitex" / "agent-container" / "runtime"),
        )
    )
    state_dir = runtime_root / name
    removed = clear_session_id(state_dir)
    if removed:
        print(
            f"[force] cleared persisted session_id for {name!r} "
            f"({state_dir / 'session_id'})",
            file=sys.stderr,
        )


def _fire_forget_hook(
    agent_name: str,
    hook_name: str,
    commands: list[str],
    context: dict | None = None,
) -> None:
    """Invoke ``run_hook`` (non-blocking, handles URL + shell entries).

    Called alongside the legacy synchronous ``_run_hooks`` path so
    existing YAML pipes/redirects keep working unchanged while
    external tools (orochi etc.) can additionally plug in via
    ``http(s)://`` URLs. The legacy path filters out URL entries to
    avoid double-dispatch of the same side-effect.
    """
    # stx-allow: fallback (reason: hook dispatch is fire-and-forget; a URL hook failing must never crash the caller)
    try:
        run_hook(agent_name, hook_name, list(commands or []), context=context)
    except Exception:  # pragma: no cover  # stx-allow: fallback (reason: hook dispatch safety net — hook crashes must not propagate to caller)
        import sys

        print(
            f"[WARN] run_hook {hook_name} dispatch failed for {agent_name}",
            file=sys.stderr,
        )


def _run_hooks(
    hooks: list[str],
    extra_env: dict[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Execute a list of shell hook commands.

    Args:
        hooks: Shell commands to execute.
        extra_env: Additional env vars passed to hook subprocesses
            (e.g., SCITEX_AGENT_CONTAINER_CONFIG_PATH, SCITEX_AGENT_CONTAINER_SCREEN_NAME, SCITEX_AGENT_CONTAINER_NAME).
        runner: Injectable subprocess runner (real callable; default
            :func:`subprocess.run`). The runner is invoked with the
            exact ``subprocess.run`` keyword surface and must return an
            object with ``returncode`` and ``stderr`` attributes.
    """
    import os

    env = {**os.environ, **(extra_env or {})}
    for hook in hooks:
        if not hook:
            continue
        # URL entries are handled by the new fire-and-forget path
        # (see _fire_forget_hook / hooks.run_hook). Skip them here to
        # avoid trying to ``sh -c "https://..."``.
        if isinstance(hook, str) and hook.startswith(("http://", "https://")):
            continue
        result = runner(hook, shell=True, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            # Log but don't fail
            import sys

            print(f"[WARN] Hook failed: {hook}", file=sys.stderr)
            if result.stderr:
                print(f"       {result.stderr.strip()}", file=sys.stderr)


def _load_handover_module() -> Any:
    """Return the real :mod:`._lifecycle.handover` module.

    Separate function so callers can substitute a real hand-rolled
    handover collaborator with the same call surface
    (``ensure_instance_uuid``, ``hydrate_from_hub``,
    ``push_pre_stop_snapshot``, ``start_failback_poller``) via the
    ``handover_mod`` kwarg on the lifecycle entry points.
    """
    from . import handover as _h

    return _h


def agent_start(
    config_path: str,
    registry: Registry | None = None,
    no_preflight: bool = False,
    force: bool = False,
    session_override: str | None = None,
    resume_id_override: str | None = None,
    dry_run: bool = False,
    foreground: bool = False,
    one_shot: bool = False,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    thread_factory: Callable[..., Any] = threading.Thread,
    handover_mod: Any = None,
) -> bool:
    """Start an agent from a YAML config file.

    Args:
        config_path: Path to the YAML agent definition.
        registry: Optional registry instance.
        no_preflight: If True, skip SSH preflight checks (useful for slow hosts).
        force: If True and the agent is already running, stop it first
            and then start fresh. Also tolerates stale registry entries
            and ghost screens (via force-stop).
        session_override: If set, override config.claude.session for this
            start invocation (one of continue | new-session | resume).
        resume_id_override: If set, override config.claude.resume_id. Pass
            with session_override="resume" to launch ``claude --resume <id>``
            without editing the YAML.
        dry_run: If True, materialize the workspace files but skip the
            multiplexer / Claude Code launch and registry registration.
            Hooks (pre_start / post_start) are also skipped.
        one_shot: If True, run startup_prompts as a single SDK turn and
            exit (sets ``--print-stream`` on the runner). Requires
            ``spec.startup_prompts`` to be non-empty.
        runtime_factory: Injectable real callable that builds an SDK
            runtime from an :class:`AgentConfig`. Default is the real
            :func:`_get_runtime`.
        sleep_fn: Injectable real sleep (default ``time.sleep``).
        thread_factory: Injectable real Thread constructor (default
            ``threading.Thread``).
        handover_mod: Injectable real handover collaborator exposing the
            module-level API of :mod:`._lifecycle.handover`. Default
            ``None`` resolves to the real module.

    Returns True on success, False on failure.
    """
    config_path = resolve_config(config_path)
    registry = registry or Registry()
    config = load_config(config_path)
    if session_override:
        config.claude.session = session_override
    if resume_id_override is not None:
        config.claude.resume_id = resume_id_override
    if one_shot and not (config.startup_prompts or config.startup_commands):
        raise RuntimeError(
            f"--one-shot requires spec.startup_prompts (or legacy "
            f"startup_commands) on agent '{config.name}'; nothing to run."
        )
    # Resolve spec.a2a.port BEFORE the runtime builds argv. ``"auto"``
    # gets a fresh allocator claim; an explicit int is recorded so
    # ``sac listen`` can find the port via state.db without re-parsing
    # the spec.yaml.
    resolve_a2a_port(config)
    runtime_factory = runtime_factory or _get_runtime
    runtime = runtime_factory(config)

    # Already running?
    if registry.exists(config.name) and runtime.is_running(config):
        if force:
            agent_stop(
                config.name,
                registry=registry,
                force=True,
                runtime_factory=runtime_factory,
                handover_mod=handover_mod,
            )
            # Small grace period so the previous container is fully torn
            # down before we try to create a new one with the same name.
            sleep_fn(1)
        elif dry_run:
            # Dry-run inspects the planned workspace even while the live
            # agent is running — the prep does not touch the container.
            pass
        else:
            # Idempotent start: re-running ``sac agent start <name>`` on
            # an agent that's already healthy is a no-op, not an error.
            # Use ``--force`` to actually restart, ``sac agent restart``
            # to be explicit, or ``sac agent stop`` then re-start.
            print(
                f"Agent '{config.name}' is already running. No-op. "
                "Use --force to restart.",
                file=__import__("sys").stderr,
            )
            return True
    elif force and registry.exists(config.name):
        # Registry says it exists but runtime says not running — stale entry.
        agent_stop(
            config.name,
            registry=registry,
            force=True,
            runtime_factory=runtime_factory,
            handover_mod=handover_mod,
        )

    # --force = "I want a clean start". Wipe the persisted SDK
    # ``session_id`` resume marker so the next runtime.start cannot
    # silently re-resume an aged-out conversation (server-side TTL is
    # finite; a stale id surfaces as
    # ``ProcessError: Command failed with exit code 1`` ~90s into the
    # first turn — see fix/start-force-clears-session-id).
    if force:
        _clear_persisted_session_id(config.name)

    # Hook env vars — let hooks know about the agent context
    hook_env = {
        "SCITEX_AGENT_CONTAINER_CONFIG_PATH": str(Path(config_path).resolve()),
        "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
        "SCITEX_AGENT_CONTAINER_NAME": config.name,
    }

    if dry_run:
        # Materialize the workspace via the runtime's dry-run path; skip
        # hooks, registry, context-manager, health monitor.
        try:
            return runtime.start(
                config, no_preflight=no_preflight, force=force, dry_run=True
            )
        except TypeError:
            # Older runtimes without dry_run support — fail loudly so
            # the caller knows this runtime can't dry-run.
            raise RuntimeError(
                f"runtime {type(runtime).__name__} does not support --dry-run"
            )

    # ZOO#12 — lead-state-handover plumbing. All three calls are
    # best-effort: missing token / 404 / network errors must NOT block
    # agent_start. ``ensure_instance_uuid`` writes
    # ``SAC_INSTANCE_UUID`` into ``config.env`` so the runtime's
    # ``_build_env_exports`` (claude_code.py) propagates it; the runtime
    # is supposed to read it back when wiring up the orochi WS connect
    # (FR-E). ``hydrate_from_hub`` is pre-start so the agent's boot-time
    # skill can pick up the snapshot before claude actually launches.
    _h = handover_mod if handover_mod is not None else _load_handover_module()

    _h.ensure_instance_uuid(config)
    try:
        _h.hydrate_from_hub(config)
    except Exception:
        # Defensive: hub_client already swallows transport errors, but
        # in case of a serialization bug here, never let agent_start
        # die because of a snapshot fetch.
        traceback.print_exc()

    # Pre-start hooks
    _run_hooks(config.hooks.get("pre_start", []), extra_env=hook_env)
    _fire_forget_hook(config.name, "pre_start", config.hooks.get("pre_start", []))

    # Start — ``force`` is propagated to the runtime. The legacy
    # ``config.remote.no_preflight`` override was retired with
    # ``RemoteSpec`` in WI-6 (handoff §6, 2026-05-20); the
    # ``--no-preflight`` CLI flag remains the only way to set it.
    start_kw = {"no_preflight": no_preflight, "force": force, "foreground": foreground}
    if one_shot:
        start_kw["one_shot"] = True
    success = runtime.start(config, **start_kw)
    if not success:
        raise RuntimeError(f"Failed to start agent '{config.name}'")

    # Register
    registry.add(
        name=config.name,
        config_path=str(Path(config_path).resolve()),
        screen_name=config.screen_name,
    )

    # Post-start hooks
    _run_hooks(config.hooks.get("post_start", []), extra_env=hook_env)
    _fire_forget_hook(config.name, "post_start", config.hooks.get("post_start", []))

    # Start health monitor in background if enabled
    if config.health.enabled:
        thread = thread_factory(
            target=health_monitor,
            args=(
                config.name,
                config,
                registry,
                lambda c: runtime_factory(c).start(c),
            ),
            daemon=True,
        )
        thread.start()

    # ZOO#12 FR-B — priority-failback poller. No-op when the spec lacks
    # a ``priority_list``; otherwise polls the hub every 60 s and steps
    # aside (snapshot push + SIGTERM) when a higher-priority host is
    # healthy. Daemon thread, dies with the process.
    try:
        _h.start_failback_poller(config)
    except Exception:
        traceback.print_exc()

    return True


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


def agent_restart(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    handover_mod: Any = None,
) -> bool:
    """Restart an agent by name.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
        sleep_fn: Real sleep (default ``time.sleep``).
        handover_mod: Real handover collaborator (default ``None``
            resolves to the real module).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    config_path = entry["config"]
    agent_stop(
        name,
        registry,
        runtime_factory=runtime_factory,
        handover_mod=handover_mod,
    )
    sleep_fn(2)
    return agent_start(
        config_path,
        registry,
        runtime_factory=runtime_factory,
        sleep_fn=sleep_fn,
        handover_mod=handover_mod,
    )


def agent_status(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> dict:
    """Get detailed status for an agent.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    # stx-allow: fallback (reason: YAML or runtime may be unavailable; status should degrade to stopped=False rather than raise)
    try:
        config = load_config(entry["config"])
        runtime = runtime_factory(config)
        running = runtime.is_running(config)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        traceback.print_exc()
        running = False
        config = None

    result = {
        "name": name,
        "config": entry.get("config", ""),
        "screen": entry.get("screen", ""),
        "started_at": entry.get("started_at", ""),
        "status": "running" if running else "stopped",
        "model": config.model if config else "unknown",
        "runtime": config.runtime if config else "unknown",
    }
    # ``config.remote`` was deleted in WI-6; spec.host (host pinning)
    # is the v3 equivalent and is recorded in state.db's ``instances``
    # table rather than echoed back through ``status``.

    # Hook-points / listen / extensions plumbing (todo#286 Phase 4).
    # Counts are exposed so consumers can see what's wired up; command
    # bodies are intentionally NOT echoed to avoid leaking URLs or
    # secrets through status --json.
    if config is not None:
        hooks = config.hooks or {}
        result["hooks_configured"] = {
            key: len(hooks.get(key, []) or [])
            for key in (
                "pre_start",
                "post_start",
                "pre_stop",
                "post_stop",
                "on_compact",
                "on_restart",
                "on_diff",
            )
        }
        result["listen"] = [
            {
                "port": lp.port,
                "proto": lp.proto,
                "path": lp.path,
                "name": lp.name,
                "owner": lp.owner,
            }
            for lp in (config.listen or [])
        ]
        # Opaque pass-through — echoed verbatim.
        result["extensions"] = dict(config.extensions or {})
    else:
        result["hooks_configured"] = {}
        result["listen"] = []
        result["extensions"] = {}

    result["context_management"] = None

    # Snapshot block — cheap read from cache (todo#286). Never re-gathers.
    # stx-allow: fallback (reason: snapshot module may not yet exist or cache may be absent on first run; None snapshot is valid initial state)
    try:
        from .._state.snapshot import read_latest

        latest = read_latest(name)
        if latest is not None:
            result["snapshot"] = {
                "timestamp": latest.get("timestamp"),
                "has_diff": latest.get("has_diff", False),
                "diff_fields": latest.get("diff_fields", []),
            }
        else:
            result["snapshot"] = None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        result["snapshot"] = None

    # Enrich with claude-hud-style metadata. Canonical source for the
    # Agents-tab dashboard; the MCP sidecar heartbeat shells out to this
    # command rather than duplicating the logic in TypeScript.
    # stx-allow: fallback (reason: agent_meta requires psutil and an active tmux session; metadata enrichment is optional and must never break status)
    try:
        from .._state.agent_meta import collect_rich

        workdir = config.expanded_workdir if config else _fallback_workdir(name)
        session = entry.get("screen", "") or (config.screen_name if config else name)
        rich = collect_rich(name=name, workdir=workdir, session=session)
        # Prefer transcript-derived started_at only if the registry
        # doesn't have one.
        if not result.get("started_at") and rich.get("started_at_transcript"):
            result["started_at"] = rich["started_at_transcript"]
        rich.pop("started_at_transcript", None)
        rich.pop("model_transcript", None)
        # Never let rich overwrite the canonical registry/config fields.
        for k, v in rich.items():
            result.setdefault(k, v)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        # Never let metadata collection break status.
        pass

    return result


def agent_logs(
    name: str,
    lines: int = 50,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> str:
    """Get recent logs from an agent.

    Args:
        name: Agent name.
        lines: Number of trailing log lines to return.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    config = load_config(entry["config"])
    runtime = runtime_factory(config)
    return runtime.logs(config, lines)
