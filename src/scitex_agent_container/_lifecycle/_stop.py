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
# previous runtime to actually exit before ESCALATING to SIGKILL (see
# :mod:`._stop_escalate`). Tuned for apptainer healthy teardown
# (~0.5–2 s); 15 s leaves comfortable headroom for a loaded host. The
# race this closes: new container boots while the old one still holds
# /home/agent overlay + per-agent stdio MCP child still holds its PID
# lock file → new bun child's ``acquireLock`` sees the live old PID and
# exits 1 → claude silently drops the MCP.
_DEFAULT_WAIT_FOR_STOP_TIMEOUT_S = 15.0


def agent_stop(
    name: str,
    registry: Registry | None = None,
    force: bool = False,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
    handover_mod: Any = None,
    prune_runtime: bool = False,
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
        prune_runtime: When True AND the agent opted in
            (``restart.policy: never`` + ``restart.prune_on_stop: true``),
            prune its runtime dir + overlay after teardown (inode
            hygiene). Default False so the internal ``agent_stop`` calls
            made by ``agent_restart`` / force-``agent_start`` NEVER prune
            the runtime they are about to reuse — only the terminal
            ``sac agents stop`` entry point passes True.
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

    # PRE-STOP RESCUE REMOVED — operator ruling 2026-07-19: 「rescue 一切やめましょう」.
    #
    # It committed every dirty worktree before an agent died. Its git module
    # claimed a "never publishes" contract, enforced by shipping no push
    # primitive. That contract was enforced against the WRONG VERB and was
    # false in production: the rescue commits onto whatever branch the agent
    # is currently on, and when that is a feature branch the commit rides a
    # normal PR merge straight into develop. Measured on 2026-07-19:
    #
    #   git log origin/develop --grep='^rescue' | wc -l   ->  7
    #   5340014c -> fix/restart-preflight-auth-before-stop      (feature branch)
    #   1042139e -> feat/a2a-default-communicate-and-role-visibility
    #
    # One of those (37d83977, ancestor of BOTH develop and main) committed
    # nine mode-160000 gitlinks under .tmp-audit/ with no .gitmodules, which
    # broke actions/checkout on every CI run of every workflow until PR #769
    # removed them. The same pollution sits in seven other repos, because a
    # broad `git add` of a dirty worktree sweeps up whatever happens to be in
    # it — audit scratch, nested clones, .worktrees/.
    #
    # A dirty worktree now simply STAYS dirty across a stop. That is the
    # intended behaviour: losing uncommitted scratch is cheaper than silently
    # publishing it.

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

    # Inode-hygiene prune (sac-runtime-state-hygiene incident). Only the
    # terminal ``sac agents stop`` entry passes ``prune_runtime=True``;
    # the gate inside further restricts to opted-in ephemeral agents
    # (``restart.policy: never`` + ``restart.prune_on_stop: true``), so
    # persistent agents are never pruned. Best-effort + fail-loud-log —
    # never raises, so it cannot break the stop path.
    if prune_runtime:
        from ._prune_runtime import maybe_prune_agent_runtime

        try:
            maybe_prune_agent_runtime(config)
        except Exception:  # stx-allow: fallback (reason: prune is best-effort teardown hygiene; any unexpected failure must not fail an otherwise-successful stop)
            traceback.print_exc()
    return True


def agent_stop_all(
    registry: Registry | None = None,
    force: bool = False,
    *,
    stop_fn: Optional[Callable[..., bool]] = None,
    prune_runtime: bool = False,
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
        prune_runtime: Forwarded to each per-agent :func:`agent_stop`
            (inode-hygiene prune for opted-in ephemeral agents).
    """
    registry = registry or Registry()
    stopper = stop_fn or agent_stop
    results: list[tuple[str, bool, str]] = []
    for entry in registry.list_all():
        name = entry.get("name", "?")
        # Forward ``prune_runtime`` only when set so an injected
        # ``stop_fn`` with the historical ``(name, registry, force)``
        # signature keeps working (the default path never opts in).
        extra = {"prune_runtime": True} if prune_runtime else {}
        # stx-allow: fallback (reason: stopping one agent may fail due to a missing config or dead session; other agents in the registry should still be stopped)
        try:
            stopper(name, registry=registry, force=force, **extra)
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
    config_resolver: Optional[Callable[[str], str]] = None,
    wait_for_stop_timeout_s: float = _DEFAULT_WAIT_FOR_STOP_TIMEOUT_S,
    successor_auth_check: Optional[Callable[[str], None]] = None,
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

    Teardown gate (2026-06-07, bug #42 — telegrammer drops after restart):
    Between ``agent_stop`` (which sends SIGTERM and returns immediately —
    ``ApptainerContainerRuntime.stop`` says "No wait loop yet — sac's
    outer lifecycle does its own poll") and ``agent_start``,
    :func:`._stop_escalate.ensure_previous_runtime_down` polls
    ``runtime.is_running`` until False or ``wait_for_stop_timeout_s``
    elapses. The legacy fixed ``sleep_fn(2)`` was the SOLE settle window —
    it raced the apptainer teardown on a loaded host. Operator-visible
    symptom: the new container booted while the old one still held the
    ``/home/agent`` overlay (apptainer warned "destination is already in
    the mount point list"), AND the old SDK's per-agent stdio MCP child
    (the standalone bun telegrammer poller) was still alive holding
    ``$HOME/.claude-code-telegrammer-*/...lock``. The new bun child's
    ``acquireLock`` then hit ``process.kill(old_pid, 0)`` SUCCESS →
    ``process.exit(1)``, claude silently marked the MCP failed and never
    retried it. ``sac`` + Mermaid MCPs reloaded fine (no inter-instance
    lock for those), masking the bug as "only Telegram broke."

    Gate ESCALATES, then FAILS LOUD (2026-07-14 — restart printed success
    over a DOWN agent): on grace-timeout the gate used to log LOUD and
    proceed into the start anyway. That WARN literally predicted the
    collision ("previous runtime still running ... proceeding to start
    anyway") which then happened ("duplicate session 'tui-neurovista'"),
    after which the CLI printed "Agent 'neurovista' restarted" over an
    agent that was left DOWN. A stop that could not stop the thing must
    not walk into a start that is guaranteed to collide with it. The gate
    now escalates SIGTERM → SIGKILL (the normal forced-stop contract,
    aimed at ``runtime.agent_pid`` — the TUI PANE pid, not the launcher
    that already exited), re-verifies with the runtime's OWN
    ``is_running``, and raises :class:`._stop_escalate.StopEscalationError`
    when it still cannot confirm the agent is down — so ``agent_start``
    below is never reached and no success line is printed. See
    :mod:`._stop_escalate`.

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
        wait_for_stop_timeout_s: SIGTERM grace for the previous-runtime
            gate (see "Teardown gate" above) before it escalates to
            SIGKILL. Default 15 s — ~10× a healthy apptainer teardown.
            Set to 0 to skip the gate entirely (legacy behaviour,
            retained for tests of unrelated code paths).

    Raises:
        RuntimeError: When ``name`` has neither a registry row NOR a
            resolvable spec — a genuinely unknown agent.
        StopEscalationError: When the previous runtime survived both
            SIGTERM and SIGKILL. The start leg is NOT run (it would
            collide with the survivor), so the agent is left as it was —
            still UP as the OLD process — and the caller reports a
            FAILED restart rather than a fictional success.
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

    # PRE-STOP auth pre-flight (INCIDENT
    # incident-agent-self-restart-one-way-20260712). Resolve + PROBE the
    # credential the SUCCESSOR container will launch on BEFORE stopping. A
    # stale-but-unexpired snapshot (future ``expiresAt`` but a
    # server-invalidated refresh_token) passes the timestamp-only launch gate,
    # boots, and 401s "Login expired" — and the stop has ALREADY happened, so
    # the dead successor cannot even report it (the one-way trip). Probing here
    # lets a REJECTED grant ABORT the restart via
    # :class:`_restart_preflight.RestartPreflightAbort` — which propagates out
    # of ``agent_restart`` so ``agent_stop`` below is NEVER reached and the
    # running container is LEFT UP. A network/endpoint failure fails OPEN (a
    # false-negative that blocks a HEALTHY restart is worse than the bug). This
    # covers the manual ``sac agents restart`` AND the listen-brokered external
    # restart (both shell ``sac agents restart`` → here); the self-restart
    # bounce (``sac agents start --force``, PR #628) is covered by the twin
    # check in ``agent_start``'s force branch. Injectable for tests.
    from ._restart_preflight import preflight_from_config_path

    _auth_check = successor_auth_check or preflight_from_config_path
    _auth_check(config_path)

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
    # Escalate (SIGKILL) or RAISE — never "proceed to start anyway" into a
    # collision this gate already knows is coming. See ._stop_escalate.
    from ._stop_escalate import ensure_previous_runtime_down

    ensure_previous_runtime_down(
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
    # ``assume_yes=True`` — a restart is an ALREADY-authorized action: the
    # ``sac agents restart`` CLI refuses without ``-y`` (see
    # ``cli_pkg/lifecycle/_restart.py``) and the MCP / public-API restart
    # paths carry the same intent, so consent is given by the time control
    # reaches here. It must be threaded into the start leg because, when
    # this runs INSIDE an apptainer SIF, ``agent_start`` brokers the start
    # to the host's ``sac listen`` ``POST /agents`` handler, which shells a
    # FRESH ``sac agents start <name>`` subprocess that re-runs the SAME
    # interactive refuse-without-``--yes`` gate
    # (``cli_pkg/lifecycle/_start_single.py::should_preview_and_require_yes``).
    # Without this, an in-SIF restart brokered through ``/agents`` refused
    # itself with "refusing to start <name> without --yes/-y" → HTTP 502,
    # even though the restart was explicitly authorized (repro 2026-07-09).
    # This does NOT weaken the human-at-a-TTY guard: a bare ``sac agents
    # start``/``restart`` with no consent still refuses — only the
    # pre-authorized restart's own start leg asserts the consent already given.
    # ``force=True`` — a RESTART's contract is to REPLACE the process, so its
    # start leg must be allowed to take over a surviving session.
    #
    # Without force, the start leg hits ``tui_session``'s duplicate-session
    # guard, which is *idempotent for a plain `sac agents start`* (an
    # already-running agent is "fine, it's up") and therefore RETURNS TRUE. For
    # a restart that verdict is a LIE: the caller asked for a new process and
    # got the old one. The failure mode was not theoretical — it happened
    # exactly when the stop leg could not kill the old runtime (SIGTERM
    # ignored) and the gate PROCEEDED ANYWAY. Then: stale session survives ->
    # start no-ops -> returns True -> the CLI prints "Agent '<name>' restarted".
    # The operator hit precisely this on neurovista: he believed it had
    # relaunched on freshly-picked credentials, was in fact still talking to the
    # OLD process on its OLD token, saw "Login expired", and went diagnosing a
    # credential store that was entirely healthy.
    #
    # Belt AND braces, because they fail differently:
    #   * the GATE above (``ensure_previous_runtime_down``) means we only reach
    #     this line once the previous runtime is CONFIRMED down — a survivor
    #     raises instead of falling through into a guaranteed collision;
    #   * ``force=True`` here means that even a session the gate could not SEE
    #     (a runtime whose ``is_running`` reads False while a stale tmux session
    #     lingers) is torn down by the start leg rather than silently no-op'd
    #     into a fictional success.
    return agent_start(
        config_path,
        registry,
        assume_yes=True,
        force=True,
        runtime_factory=runtime_factory,
        sleep_fn=sleep_fn,
        handover_mod=handover_mod,
    )
