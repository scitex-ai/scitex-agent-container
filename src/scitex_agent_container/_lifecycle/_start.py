"""``agent_start`` — local/remote agent launch.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports ``agent_start``.
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config, resolve_config

# Re-exported for back-compat: these ran inline in ``agent_start`` before the
# pre-launch region moved to ``_start_prelaunch`` (512-line cap), and callers /
# tests import them from here. The CALLS now live in that module.
from ._a2a_port import resolve_a2a_port  # noqa: F401
from ._handover_loader import _load_handover_module
from ._hook_runner import _fire_forget_hook, _run_hooks
from ._identity_drift import check_board_identity_at_launch  # noqa: F401
from ._instances import record_local_instance as _record_local_instance
from ._layers_preflight import check_to_home_layers_at_launch  # noqa: F401
from ._runtime_select import _get_runtime
from ._session_reset import _clear_persisted_session_id
from ._spawn_gate import enforce_spawn_gate, persist_acl_policy  # noqa: F401

# Re-exported from _start_announce for back-compat: this helper lived here
# before the 512-line-cap split.
from ._start_announce import _announce_start_verdict  # noqa: F401
from ._start_failure_diag import _format_boot_stderr_section  # noqa: F401
from ._start_outcome import NOOP_ALREADY_RUNNING

# Re-exported from _start_preflight for back-compat: callers/tests import these
# pre-flight helpers from _start. ``_verify_real_liveness`` is no longer the
# no-op GATE (see :mod:`._start_verdict` — it is now one signal among several,
# and its ``False`` no longer means "dead"), but it remains a supported helper
# with its own tests. _resolve_strict_drift is used only transitively.
from ._start_preflight import (  # noqa: F401
    _check_spec_source_drift_at_launch,
    _resolve_strict_drift,
    _rotate_to_healthy_account,
    _verify_real_liveness,
)
from ._start_prelaunch import run_prelaunch
from ._start_supervision import start_background_supervision
from ._worktree_policy import enforce_task_worktree_policy


def _should_clear_persisted_session(
    *, force: bool, explicit_session_override: str | None
) -> bool:
    """Separate process replacement from explicit conversation replacement."""
    return force and explicit_session_override == "fresh"


def agent_start(
    config_path: str,
    registry: Registry | None = None,
    force: bool = False,
    *,
    session_override: str | None = None,
    resume_id_override: str | None = None,
    engine_override: str | None = None,
    probe_engine: bool | None = None,
    dry_run: bool = False,
    no_preflight: bool = False,
    foreground: bool = False,
    one_shot: bool = False,
    assume_yes: bool = False,
    strict_drift: bool | None = None,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    thread_factory: Callable[..., Any] = threading.Thread,
    handover_mod: Any = None,
    liveness_verifier: Callable[[AgentConfig, Any], bool] | None = None,
    verdict_override: Any = None,
    in_sif_opener: Optional[Callable[..., Any]] = None,
    successor_auth_check: Callable[[AgentConfig], None] | None = None,
    managed_turn_probe: Callable[[AgentConfig], Any] | None = None,
    stop_instance_resolver: Callable[[AgentConfig, Any], dict | None] | None = None,
) -> bool:
    """Start an agent from its config YAML.

    Args:
        config_path: Path to the agent's spec.yaml.
        registry: Optional registry instance.
        force: Restart even when the agent is already running.
        session_override: Override ``spec.claude.session``.
        resume_id_override: Override ``spec.claude.resume_id``.
        engine_override: Select a DIFFERENT ``spec.engines`` entry for
            THIS start (the CLI ``--engine <key>``). ``None`` uses the
            spec's declared default engine. An unknown key raises rather
            than degrading to the default, and an engine that cannot be
            honoured refuses the start rather than falling back — see
            :mod:`._engine_select` (operator answers Q2 / Q3).
        probe_engine: Whether to run the OPT-IN live reachability probe
            of the selected engine's ``base_url``. ``None`` defers to
            ``SAC_ENGINE_PROBE``, whose default is OFF: STATIC
            resolution runs on every start and is the whole refusal
            surface by default, because making every start depend on a
            possibly-remote endpoint answering is how a refusal grounds
            a fleet. Even with the probe on, only an ACTIVE connection
            refusal refuses; a timeout is "could not tell" and warns.
        dry_run: Materialize the workspace without launching the agent.
        no_preflight: Skip the runtime preflight (CLI ``--no-preflight``).
        foreground: Run the runtime in the foreground.
        one_shot: Run the startup prompts once and exit; requires
            ``spec.startup_prompts`` to be non-empty.
        assume_yes: The CLI caller's own ``-y``/``--yes`` consent,
            propagated to the in-SIF broker (bug fix 2026-07-05,
            paper-scitex-clew report). When ``agent_start`` is invoked
            from inside an apptainer SIF, the spawn is brokered to the
            host's ``sac listen`` (see :func:`_in_sif_broker.
            maybe_broker_in_sif_spawn`), which shells a FRESH
            ``sac agents start <name>`` on the bare host — a subprocess
            that re-runs the same interactive refuse-without-``--yes``
            gate the ORIGINAL caller already satisfied. Without
            threading this through, that consent never reached the host
            subprocess and every brokered start refused itself even
            though ``-y`` was explicitly passed at the CLI. Ignored on
            the non-SIF (direct) path — it has no interactive gate of
            its own to satisfy.
        strict_drift: Compatibility-only input. Spec authority validation is
            always strict; ``False`` and legacy environment variables cannot
            bypass it. Intentional detached snapshots are accepted only by
            exact immutable source/commit/spec-digest policy.
        runtime_factory: Injectable real callable that builds an SDK
            runtime from an :class:`AgentConfig`. Default is the real
            :func:`_get_runtime`.
        sleep_fn: Injectable real sleep (default ``time.sleep``).
        thread_factory: Injectable real Thread constructor (default
            ``threading.Thread``).
        handover_mod: Injectable real handover collaborator exposing the
            module-level API of :mod:`._lifecycle.handover`. Default
            ``None`` resolves to the real module.
        managed_turn_probe: Native activity observer for an internal process
            replacement. The stop guard always preserves active Hermes turns.
        stop_instance_resolver: Resolve the exact instance during an internal
            process replacement; defaults to the stop backend's resolver.

    Truthy on success, False on failure; the already-running no-op
    returns the tagged ``_start_outcome.NOOP_ALREADY_RUNNING``.
    """
    config_path = resolve_config(config_path)
    registry = registry or Registry()
    config = load_config(config_path)

    # Nested apptainer launches are unsupported: broker them to the host,
    # which checks spawn authority and records lineage. Dry-run stays local.
    from ._in_sif_broker import is_in_sif, maybe_broker_in_sif_spawn

    # The spawn broker refuses obsolete replacement requests; container-side
    # restarts use the dedicated restart endpoint. Foreground/one-shot/consent
    # are forwarded, but engine and resume IDs are unsupported by this wire
    # contract. Refuse those requests before dropping their explicit intent.
    if engine_override and not dry_run and is_in_sif():
        raise RuntimeError(
            f"--engine {engine_override!r} cannot be honoured from inside "
            "an apptainer SIF: the start is brokered to the host's `sac "
            "listen`, whose request body has no engine field, so the "
            "engine would be silently dropped and the agent would start "
            "on its DEFAULT engine. sac refuses to start rather than "
            f"start on a backend you did not ask for. Run on the host: "
            f"sac agents restart {config.name} --yes --engine "
            f"{engine_override}"
        )
    if resume_id_override and not dry_run and is_in_sif():
        raise RuntimeError(
            "an explicit --resume ID cannot be carried by this host spawn protocol; "
            "run the exact resume command on the owning host"
        )
    if maybe_broker_in_sif_spawn(
        config.name,
        dry_run=dry_run,
        opener=in_sif_opener,
        foreground=foreground,
        one_shot=one_shot,
        assume_yes=assume_yes,
        force=force,
        session=session_override,
    ):
        return True

    # The PRE-LAUNCH GAUNTLET — every gate that must hold before the
    # runtime is built and before any forced stop, so a refusal never
    # tears down a running agent it cannot bring back: the two
    # spec-sanity gates, board identity, credential rotation, the
    # start-time overrides (session / resume id / ENGINE), the one-shot
    # requirement, the spawn ACL gate, the ACL policy publish, the a2a
    # port, and the telegrammer wake wiring. Body lives in
    # ``_start_prelaunch`` (per-file line cap); the region moved verbatim.
    run_prelaunch(
        config,
        config_path,
        strict_drift=strict_drift,
        session_override=session_override,
        resume_id_override=resume_id_override,
        engine_override=engine_override,
        probe_engine=probe_engine,
        one_shot=one_shot,
        dry_run=dry_run,
    )

    uses_production_runtime = runtime_factory is None
    runtime_factory = runtime_factory or _get_runtime
    runtime = runtime_factory(config)

    if uses_production_runtime and not dry_run:
        from ..runtimes.tui_session import TuiSessionRuntime

        if isinstance(runtime, TuiSessionRuntime):
            from .._state.state_store_instances_store import (
                ensure_instances_ownership_schema,
            )

            ensure_instances_ownership_schema()

    # Lazy import breaks the ``_start`` <-> ``_stop`` cycle (restart cleanup
    # stops here; ``agent_restart`` starts there).
    from ._stop import agent_stop

    # Already running?
    forced_stop = False
    # Stale-lease cleanup (operator pain point — replaces the manual
    # ``DELETE FROM instances`` workaround). When the runtime PID
    # is dead, any active ``instances`` row for this agent name is stale (the
    # previous container died without going through agent_stop). Clear those
    # rows so a zombie lease cannot vouch for a dead agent. Live runtimes are
    # NEVER touched — the gate is the precondition.
    if not runtime.is_running(config):
        from ._stale_lease import clear_stale_instance_lease

        clear_stale_instance_lease(config.name)

    # Only observed ALIVE pins the no-op; UNKNOWN proceeds to the runtime's
    # duplicate-session guard without destroying a live session. See
    # _start_verdict for the decision rule. verdict_override supplies a real
    # observation when testing this wiring independently of the instruments.
    from ._start_verdict import resolve_start_verdict

    verdict = (
        verdict_override
        if verdict_override is not None
        else resolve_start_verdict(
            config, runtime, registry=registry, liveness_verifier=liveness_verifier
        )
    )
    really_running = verdict.is_alive
    if uses_production_runtime and (dry_run or not really_running or force):
        enforce_task_worktree_policy(config, provision=not dry_run)
    if really_running:
        from ._start_engine_noop import assert_explicit_engine_noop_safe

        assert_explicit_engine_noop_safe(
            config, engine_override, force=force, dry_run=dry_run
        )
    if not really_running and not dry_run:
        _announce_start_verdict(verdict)
    if uses_production_runtime and force and not dry_run:
        from ..runtimes._native_tui_admission import preflight_native_tui

        preflight_native_tui(config, production=True)
    if really_running:
        if force:
            # PRE-STOP auth pre-flight (INCIDENT self-restart-one-way-
            # 20260712): probe the already-rotated successor credential; a
            # REJECTED grant raises RestartPreflightAbort BEFORE agent_stop so
            # the live container is LEFT UP. Internal restart cleanup also
            # guards activity again in case another process appeared meanwhile.
            from ._restart_preflight import assert_successor_auth_usable

            _auth_check = successor_auth_check or assert_successor_auth_usable
            _auth_check(config)
            agent_stop(
                config.name,
                registry=registry,
                force=True,
                runtime_factory=runtime_factory,
                handover_mod=handover_mod,
                allow_active_turn_kill=False,
                managed_turn_probe=managed_turn_probe,
                stop_instance_resolver=stop_instance_resolver,
            )
            forced_stop = True
            # Small grace period so the previous container is fully torn
            # down before we try to create a new one with the same name.
            sleep_fn(1)
        elif dry_run:
            # Dry-run inspects the planned workspace even while the live
            # agent is running — the prep does not touch the container.
            pass
        else:
            # INFO, not SUCC: the requested end state holds, but nothing was
            # launched — so the line must not read as an accomplishment.
            # The verdict evidence stays, and the notice NAMES the session
            # (+ pane pid) it believed in: a no-op that says only "already
            # running" is unfalsifiable from the outside, and the operator
            # could not tell an OBSERVED agent from a process-shaped shadow
            # (incident 2026-08-14: a prefix-matched SIBLING session pinned
            # this branch — see _start_noop_notice).
            from ..cli_pkg._helpers._console import system_msg
            from ._start_noop_notice import render_start_noop_notice

            system_msg(
                render_start_noop_notice(config, verdict),
                style="info",
            )
            from ._startup_failed import retract_marker_for

            retract_marker_for(config.name)
            return NOOP_ALREADY_RUNNING
    elif force and registry.exists(config.name):
        # Registry says it exists but runtime says not running — stale entry.
        agent_stop(
            config.name,
            registry=registry,
            force=True,
            runtime_factory=runtime_factory,
            handover_mod=handover_mod,
            allow_active_turn_kill=False,
            managed_turn_probe=managed_turn_probe,
            stop_instance_resolver=stop_instance_resolver,
        )
        forced_stop = True

    # Cleanup releases the port allocated above. Re-establish its claim before
    # recording the successor, or the new row would have a2a_port=None and
    # inbound turn routing would fail. Resolution preserves config.a2a.port.
    if forced_stop:
        resolve_a2a_port(config)

    # Process replacement and conversation replacement are independent.
    # Internal restart cleanup replaces the process; only an explicitly
    # requested ``--fresh`` session override may wipe the conversation.
    if _should_clear_persisted_session(
        force=force,
        explicit_session_override=session_override,
    ):
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
    # is supposed to read it back when wiring up the hub WS connect
    # (FR-E). ``hydrate_from_hub`` is pre-start so the agent's boot-time
    # skill can pick up the snapshot before claude actually launches.
    _h = handover_mod if handover_mod is not None else _load_handover_module()

    launch_incarnation = _h.ensure_instance_uuid(config)
    if uses_production_runtime:
        from ._worktree_policy import refresh_task_worktree_owner

        refresh_task_worktree_owner(config)
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

    # Pre-start orphan MCP-child cleanup (bug "MCP-on-restart 409 orphan").
    # Scan for any stdio-MCP server child belonging to this agent's PREVIOUS
    # incarnation (matched by env-injected agent name + MCP-cmdline marker)
    # and SIGKILL it BEFORE we boot the runtime's replacement poller. Without
    # this, an orphaned ``claude-code-telegrammer`` poller continues to hold
    # Telegram's getUpdates long-poll slot, the new poller hits HTTP 409
    # ("terminated by other getUpdates request"), and the operator sees
    # "telegrammer dead, agent alive but silent". The helper NEVER raises —
    # orphan cleanup is best-effort defence and must not wedge start. See
    # :mod:`._orphan_mcp_cleanup` for the match policy and seam contract.
    from ._orphan_mcp_cleanup import kill_orphan_mcp_children

    kill_orphan_mcp_children(config.name)

    # Reap detached durable-inbox consumers left by an older launch or by the
    # pre-rename Hermes bridge lifecycle.  Exact agent/spec/module matching is
    # required; no command-substring pkill is used.  At this point singleton
    # teardown is complete, so this launch has no authoritative sidecar yet.
    from ..runtimes._inbox_sidecar_reconcile import reconcile_inbox_sidecars
    from ..runtimes.tui_session import state_dir_for_config

    reconcile_inbox_sidecars(
        name=config.name,
        config_path=str(getattr(config, "config_path", "") or config_path),
        incarnation_id=(
            str(launch_incarnation or config.env.get("SAC_INSTANCE_UUID") or "") or None
        ),
        state_dir=state_dir_for_config(config),
    )

    # Spec-pinned session resume (``spec.claude.session: resume`` +
    # ``spec.claude.resume_id``). Seed the SDK runner's on-disk resume
    # marker from the pinned uuid — but ONLY when no marker exists yet
    # (first boot / migration). Must run BEFORE ``runtime.start`` so the
    # in-container runner sees the seeded id on its first resume attempt.
    # Seed-if-absent preserves a later SDK fork (see ``_session_seed``).
    from ._session_seed import seed_pinned_session_id

    seed_pinned_session_id(config, runtime)

    # Twin context-inheritance (``sac agents twin``). When this spec carries
    # ``SAC_FORK_PARENT`` in its env it is a twin: resolve the parent's
    # CURRENT session uuid, pin this twin's resume to it, and copy the
    # parent's transcript into the twin's container-home projects store so
    # the resume finds it. Host-side (paths always resolve on the bare host)
    # and a strict no-op for every non-twin start. Fail-loud (TwinSeedError)
    # when the parent has no resolvable live session — a twin with no context
    # to inherit is pointless. See ``_twin.seed_twin_from_parent``.
    from ._twin import seed_twin_from_parent

    seed_twin_from_parent(config, runtime)

    # Start — ``force`` is propagated to the runtime. The legacy
    # ``config.remote.no_preflight`` override was retired with
    # ``RemoteSpec`` in WI-6 (handoff §6, 2026-05-20); the
    # ``--no-preflight`` CLI flag remains the only way to set it.
    start_kw = {"no_preflight": no_preflight, "force": force, "foreground": foreground}
    if one_shot:
        start_kw["one_shot"] = True
    success = runtime.start(config, **start_kw)
    if not success:
        # Fail loud: a bare False from runtime.start() must not become a
        # cause-less "Failed to start". Capture + persist the pane / inner
        # stderr, then raise with the real cause attached. See
        # :mod:`._start_failure_diag`.
        from ._start_failure_diag import raise_start_failure

        raise_start_failure(config)

    # Register
    registry.add(
        name=config.name,
        config_path=str(Path(config_path).resolve()),
        screen_name=config.screen_name,
    )

    # Record the state.db ``instances`` row for a LOCAL start. The
    # cross-host dispatcher (cli_pkg/lifecycle/_dispatch.py) writes the
    # lead-side row for remote agents; local starts had no row at all,
    # so ``send_to_agent`` / ``agent_send`` reported "agent not running"
    # and the /v1/turn endpoint was unreachable even though the sidecar
    # was bound. The resolved a2a_port comes from the allocator (set by
    # ``resolve_a2a_port`` above) so /v1/turn routing has the port.
    _record_local_instance(config, runtime)

    # Post-start hooks
    _run_hooks(config.hooks.get("post_start", []), extra_env=hook_env)
    _fire_forget_hook(config.name, "post_start", config.hooks.get("post_start", []))

    # TELEGRAM-RAIL VERDICT (card sac-cct-rail-loud-when-no-slot-resolves-
    # 20260812) + TOKEN-OWNERSHIP LEDGER. Both run HERE, after ``runtime.start``
    # materialised the agent's ``$HOME/.env`` — that file is precedence #1 of
    # the token resolution, so reading it earlier would report an agent as
    # token-less that in fact has one. A spec that declares the telegrammer MCP
    # but resolves NO slot has its MCP server removed (correctly, by operator
    # ruling) and the agent then starts perfectly, reports healthy, and is MUTE
    # and DEAF on Telegram with no signal anywhere; the ledger separately
    # records WHICH bot this agent took, so "who holds this one?" is a query
    # rather than a 409 from Telegram. NEITHER gates the start, and neither
    # raises. See :mod:`..runtimes._cct_start_observers`.
    from ..runtimes._cct_start_observers import observe_cct_at_start

    observe_cct_at_start(config)

    # Daemon supervisors for an agent that is now up — health monitor +
    # priority-failback poller. See :mod:`._start_supervision`.
    start_background_supervision(
        config,
        registry=registry,
        runtime_factory=runtime_factory,
        handover=_h,
        thread_factory=thread_factory,
    )

    return True
