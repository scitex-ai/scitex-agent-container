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
from ._a2a_port import resolve_a2a_port
from ._handover_loader import _load_handover_module
from ._hook_runner import _fire_forget_hook, _run_hooks
from ._instances import record_local_instance as _record_local_instance
from ._runtime_select import _get_runtime
from ._session_reset import _clear_persisted_session_id
from ._spawn_gate import enforce_spawn_gate, persist_acl_policy

# Re-exported from _start_preflight for back-compat: callers/tests import these
# pre-flight helpers from _start. _resolve_strict_drift is used only transitively
# (by _check_spec_source_drift_at_launch) but stays re-exported here.
from ._start_preflight import (  # noqa: F401
    _check_spec_source_drift_at_launch,
    _resolve_strict_drift,
    _rotate_to_healthy_account,
    _verify_real_liveness,
)
from .health import health_monitor


def agent_start(
    config_path: str,
    registry: Registry | None = None,
    force: bool = False,
    *,
    session_override: str | None = None,
    resume_id_override: str | None = None,
    dry_run: bool = False,
    no_preflight: bool = False,
    foreground: bool = False,
    one_shot: bool = False,
    strict_drift: bool | None = None,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    thread_factory: Callable[..., Any] = threading.Thread,
    handover_mod: Any = None,
    liveness_verifier: Callable[[AgentConfig, Any], bool] | None = None,
    in_sif_opener: Optional[Callable[..., Any]] = None,
) -> bool:
    """Start an agent from its config YAML.

    Args:
        config_path: Path to the agent's spec.yaml.
        registry: Optional registry instance.
        force: Restart even when the agent is already running.
        session_override: Override ``spec.claude.session``.
        resume_id_override: Override ``spec.claude.resume_id``.
        dry_run: Materialize the workspace without launching the agent.
        no_preflight: Skip the runtime preflight (CLI ``--no-preflight``).
        foreground: Run the runtime in the foreground.
        one_shot: Run the startup prompts once and exit; requires
            ``spec.startup_prompts`` to be non-empty.
        strict_drift: Escalate a drifted spec-source git repo from a
            loud warning to a hard block (raise before launch). ``None``
            (default) reads ``SAC_STRICT_DRIFT`` / ``--strict-drift`` is
            not set; ``True`` forces strict, ``False`` forces lenient.
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

    # SAC-from-SAC broker (operator-mandated 2026-06-01). When running
    # INSIDE an apptainer SIF, apptainer-in-apptainer is unsupported on
    # the deployment shape we target — POST the spawn to bare-host
    # ``sac listen`` instead. The host re-runs ``check_spawn`` + records
    # lineage + shells the real ``sac agent start``. Bypassed on
    # dry-run (dry-run inspects the LOCAL planned workspace). Fail-loud
    # contract lives in :func:`_in_sif_broker.maybe_broker_in_sif_spawn`.
    from ._in_sif_broker import maybe_broker_in_sif_spawn

    # PR-α (lead msg d96a468c 2026-06-06): propagate --foreground /
    # --one-shot through the broker so the host listen's /agents handler
    # appends them to its inner `sac agents start` argv. The cohort
    # one-shot capsule runs synchronously → real rc + real stderr land
    # in STARTUP_FAILED on crash (the diagnostic clew needs to find
    # WHY the bm172 capsule dies after one heartbeat).
    if maybe_broker_in_sif_spawn(
        config.name,
        dry_run=dry_run,
        opener=in_sif_opener,
        foreground=foreground,
        one_shot=one_shot,
    ):
        return True

    # Launch-time LOCAL spec-source drift check. Verifies the git repo
    # backing this spec.yaml (on these hosts ``~/.scitex/agent-container/
    # agents`` symlinks into ``~/.dotfiles``) is current with its remote.
    # Stale (BEHIND) → may run an old spec; unpushed (AHEAD/DIVERGED) →
    # won't propagate. Default = LOUD WARNING, never a block (hosts like
    # spartan legitimately carry local commits). ``--strict-drift`` /
    # ``SAC_STRICT_DRIFT=1`` escalate to a hard block. Always best-effort:
    # a non-git source / unreachable remote / any error warns-and-continues
    # — the check never crashes a launch (resilience is the contract).
    _check_spec_source_drift_at_launch(config_path, config.name, strict_drift)

    # CREDS-PHASE1 — auto-rotate ``spec.claude.account`` to a healthy
    # stored account when the pinned one's snapshot is EXPIRED/ABSENT.
    # Runs before forced_stop / runtime build so a "no healthy account"
    # error never tears down a running agent we cannot restart. Unpinned
    # agents (account="") are untouched: they continue to use the host
    # live ``.credentials.json`` via the existing bind. See
    # :func:`_rotate_to_healthy_account` for the contract.
    _rotate_to_healthy_account(config)

    if session_override:
        config.claude.session = session_override
    if resume_id_override is not None:
        config.claude.resume_id = resume_id_override
    if one_shot and not (config.startup_prompts or config.startup_commands):
        raise RuntimeError(
            f"--one-shot requires spec.startup_prompts (or legacy "
            f"startup_commands) on agent '{config.name}'; nothing to run."
        )

    # Spawn-permission gate + lineage record (ADR-0010 Rule B / Phase 2:
    # "起動経路 = 記録経路 = ACL経路" collapsed to one path). EVERY spawn
    # path funnels through core ``agent_start`` — the MCP ``agent_start``
    # tool and the plain ``sac agents start`` CLI both reach here, not
    # just the ``sac listen`` ``POST /agents`` handler. Enforcing the
    # gate here (rather than only in the server handler) means an
    # agent-from-agent spawn is ACL-gated WITHOUT requiring a running
    # ``sac listen`` daemon — clew on Spartan can spawn capsule children
    # with no extra process. The caller identity is the parent agent's
    # ``SAC_NAME`` env (``None`` → admin / operator / lead → always
    # allowed). On allow with a real caller, the ``caller → child`` edge
    # is written to the ``lineage`` table — the same identity that
    # ``record_local_instance`` records as ``instances.spawned_by``, so
    # the two are no longer split-brained. A denied spawn raises
    # ``SpawnDeniedError`` HERE, before the runtime is built or touched.
    # The server handler still passes its request ``caller`` verbatim and
    # records lineage itself; its subprocess inherits no ``SAC_NAME`` on
    # the bare host, so this gate sees ``caller=None`` (admin) and does
    # not double-record — and ``record_lineage`` is idempotent regardless.
    enforce_spawn_gate(config.name)

    # Phase-3 (ADR-0010 Step 2) — publish the loaded spec's per-spec
    # ACL policy into ``node_comms_policy`` so check_send_acl /
    # check_spawn / derive_group see the current outbound, inbound,
    # group=solitary, and may_spawn rules on the next request. The
    # upsert is idempotent and re-runs on every start so a spec edit
    # becomes live without manual state.db surgery. Defaults preserve
    # pre-Phase-3 behaviour, so an existing YAML with no spec.comms /
    # spec.lineage blocks writes the all-allow / may_spawn=True row.
    persist_acl_policy(config)

    # Resolve spec.a2a.port BEFORE the runtime builds argv. ``"auto"``
    # gets a fresh allocator claim; an explicit int is recorded so
    # ``sac listen`` can find the port via state.db without re-parsing
    # the spec.yaml.
    resolve_a2a_port(config)

    # Bug #41 preflight — refuse to start when spec.claude.channels
    # requests ``server:claude-code-telegrammer`` but spec.a2a.port is
    # unset/null. Without the /v1/turn endpoint the standalone
    # telegrammer poller has no URL to POST inbound Telegram to and an
    # idle agent silently won't wake. Catching this here makes the
    # misconfig loud at ``sac agents start`` time rather than the
    # operator discovering it via "agent doesn't reply to Telegram"
    # three messages later. See ``runtimes/_sdk_channels.
    # validate_telegrammer_wake_wiring`` for the contract; F3 (MCP key
    # mis-keyed in to_home/.mcp.json) is covered by the matching
    # runner-side WARN/ERROR logs in ``_wire_telegrammer_wake``.
    from ..runtimes._sdk_channels import validate_telegrammer_wake_wiring

    validate_telegrammer_wake_wiring(
        getattr(config.claude, "channels", None),
        getattr(config.a2a, "port", None),
        agent_name=config.name,
    )

    runtime_factory = runtime_factory or _get_runtime
    runtime = runtime_factory(config)

    # Lazy import breaks the ``_start`` <-> ``_stop`` cycle (force-restart
    # stops here; ``agent_restart`` starts there).
    from ._stop import agent_stop

    # Already running?
    forced_stop = False
    # Bug 1 (real-liveness): three independent signals must all agree
    # before we trust the "already-running" no-op branch. registry +
    # runtime.is_running is not enough — see :func:`_verify_real_liveness`
    # for the false-positive cases the third signal closes.
    _verify = (
        liveness_verifier if liveness_verifier is not None else _verify_real_liveness
    )
    # Stale-lease cleanup (operator pain point — replaces the manual
    # ``sqlite3 … DELETE FROM instances …`` workaround). When the
    # runtime PID is dead, any active ``instances`` row for this agent
    # name is stale (the previous container died without going through
    # agent_stop). Clear those rows so the third liveness signal does
    # not pin the no-op branch on a zombie lease. Live runtimes are
    # NEVER touched — the gate is the precondition.
    if not runtime.is_running(config):
        from ._stale_lease import clear_stale_instance_lease

        clear_stale_instance_lease(config.name)
    really_running = (
        registry.exists(config.name)
        and runtime.is_running(config)
        and _verify(config, runtime)
    )
    if really_running:
        if force:
            agent_stop(
                config.name,
                registry=registry,
                force=True,
                runtime_factory=runtime_factory,
                handover_mod=handover_mod,
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
        forced_stop = True

    # Re-establish the A2A port claim after a ``--force`` ``agent_stop``.
    # ``agent_stop`` calls ``release_a2a_port`` which DELETEs the claim
    # row that the ``resolve_a2a_port`` above inserted. Without this,
    # ``record_local_instance`` reads ``get_port(name)`` from the now-empty
    # claim table → the ``instances`` row is written with ``a2a_port=None``
    # and ``/v1/turn`` routing (which reads that row) fails after a
    # ``sac agents start --force`` restart. ``resolve_a2a_port`` is
    # idempotent: it re-INSERTs the same int already held in
    # ``config.a2a.port``, keeping the ``a2a_ports`` claim table,
    # ``config.a2a.port``, and the ``instances`` row all consistent.
    if forced_stop:
        resolve_a2a_port(config)

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
        # cause-less "Failed to start". Capture the agent's tmux pane (the
        # inner apptainer/claude output) + whether a session exists, so the
        # real boot failure — boot-drain timeout, auth, a broken in-container
        # login shell, an immediate claude exit — is visible, not swallowed.
        diag = ""
        try:
            from .._runners._tmux.tmux import TmuxManager
            from ..runtimes.tui_session import session_name_for

            _sess = session_name_for(config)
            _pane = TmuxManager.capture_logs(_sess, lines=60).rstrip()
            diag = (
                f" (tmux session_exists={TmuxManager.exists(_sess)})\n"
                f"  pane tail:\n{_pane or '<empty — inner process exited before any output>'}"
            )
        except Exception:  # stx-allow: fallback (reason: diagnostics must never mask the real start failure — degrade to no pane)
            diag = " (no pane diagnostics available)"
        raise RuntimeError(
            f"Failed to start agent '{config.name}': runtime.start() returned "
            f"False.{diag}"
        )

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
