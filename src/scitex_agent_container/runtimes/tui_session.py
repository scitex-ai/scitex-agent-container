"""``tui`` runtime adapter — SDK-pool-cutoff TUI pivot (hedge).

``spec.runtime: tui`` selects this adapter (opt-in per agent; the default
``claude-agent-sdk`` path is untouched). The bundled ``claude`` CLI runs
interactively inside a detached tmux session that sac owns, INSIDE apptainer —
same isolation/binds/overlay/auth/to_home delivery as ``ClaudeSessionRuntime``
(assembled by :func:`_apptainer_build_argv.build_run_argv` with ``tui=True``).
tmux is a PTY holder only (it never runs ``claude`` on the host); it lets the
agent survive operator detach and gives the inner TUI a PTY.

This module is a thin adapter — it owns the session-name convention and the
``RuntimeBase`` surface, and delegates: tmux mechanics → ``_runners/_tmux/``;
$HOME materialisation → :mod:`_tui_workspace`; modal drain → :mod:`_tui_drain`;
compose-buffer clear / submit-verify → :mod:`_tui_compose`; startup-prompt
injection → :mod:`_tui_inject`; liveness decisions → :mod:`_tui_liveness`.

``is_running`` is a LIVENESS probe (session exists AND its pane process is
alive); ``is_responsive`` is the separate ``session_activity``-freshness signal
for hang-detection (see :mod:`_tui_liveness`).
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any

from .._runners._tmux.tmux import (
    TmuxManager,
    TuiInputNotReadyError,
)
from ..config import AgentConfig
from . import _tui_delivery
from ._apptainer_build_argv import build_run_argv
from ._tui_auth_stage import TuiAuthStageError
from ._tui_boot_drain import TuiBootDrainMixin
from ._tui_bridge_seam import TurnBridgeSeamMixin
from ._tui_compose import (
    _compose_pending_live,
    clear_compose_buffer,
    verify_submit_by_advancement,
)
from ._tui_drain import drain_modals_until_ready
from ._tui_inject import StartupPromptInjectorMixin
from ._tui_liveness import (
    is_responsive_from_activity,
    pane_pid_of,
    pane_process_alive,
)
from ._tui_workspace import materialize_workspace as _materialize_workspace
from .base import RuntimeBase

__all__ = [
    "TuiAuthStageError",
    "TuiInputNotReadyError",
    "TuiSessionRuntime",
    "_compose_pending_live",
    "clear_compose_buffer",
    "drain_modals_until_ready",
    "session_name_for",
    "start_succeeded",
    "state_dir_for_config",
    "verify_submit_by_advancement",
]


_CLAUDE_BIN_DEFAULT = "claude"

# Default max-idle window for the RESPONSIVENESS probe
# (``TuiSessionRuntime.is_responsive``; liveness/``is_running`` no longer
# uses it). 300s mirrors the SDK ``health.interval`` default so a
# quiet-but-healthy TUI has grace. Overridable per-call via ``max_idle_s``.
_DEFAULT_MAX_IDLE_S = 300.0

# Boot-drain window when ``spec.startup_commands`` delay ``exec claude``
# (e.g. an in-container ``uv pip install`` that runs for minutes before
# the TUI launches). The drain polls through the bootstrap and dismisses
# claude's first-run modals (bypass-permissions / trust / theme) the
# moment they appear; it returns as soon as claude is up, so this is a
# CAP, not a fixed block. 240s comfortably covers a cold uv resolve.
_STARTUP_BOOT_DRAIN_S = 240.0


def start_succeeded(
    *,
    session_alive: bool,
    reached_ready: bool | None,
    is_running: bool,
) -> bool:
    """Pure decision: did the TUI start actually succeed? (BUG 3 — no false success.)

    ``tmux new-session`` succeeding is NOT success — the inner claude can die
    mid-boot (e.g. an Escape cancelled the dev-channels modal) or hang before
    binding its input. This function encodes the fail-loud contract so it is
    unit-testable without a live TUI:

      * ``session_alive`` False → FAILURE. A dead tmux session can never be a
        running agent, whatever the drain thought.
      * ``session_alive`` True and ``reached_ready`` True → SUCCESS. The
        boot-drain observed the input-ready / ``bypass permissions`` marker.
      * ``session_alive`` True and ``reached_ready`` False → FAILURE. The drain
        RAN and did NOT reach ready within its window (the modal never cleared /
        claude never bound input) — loud failure, not a claimed success.
      * ``session_alive`` True and ``reached_ready`` None (drain disabled this
        boot) → defer to ``is_running`` (the liveness probe): alive+responsive
        counts as success, otherwise failure.
    """
    if not session_alive:
        return False
    if reached_ready is True:
        return True
    if reached_ready is False:
        return False
    return is_running


def session_name_for(config: AgentConfig) -> str:
    """Return the tmux session name owned by sac for this agent.

    ``tui-<agent-name>`` namespace-prefixes the session so it cannot collide
    with operator-owned tmux sessions; ``TmuxManager.exists`` keys off the same
    string so the runtime probes its own session deterministically.
    """
    return f"tui-{config.name}"


def state_dir_for_config(config: AgentConfig) -> Path:
    """Per-agent state dir on the host: project-local if the agent's spec lives
    under a project-scope ``.scitex/agent-container/`` tree, else
    ``~/.scitex/agent-container/runtime/<name>/``. Mirrors
    ``ClaudeSessionRuntime._state_dir`` so both runtimes land per-agent files in
    the same place.
    """
    from .._runners import claude_session as _runner

    src = getattr(config, "config_path", "") or ""
    root: Path | None = None
    if src:
        try:
            from scitex_config._ecosystem import local_state

            scope = local_state.find_project_scope(
                "agent-container", start=Path(src).parent
            )
            if scope is not None:
                root = scope / "runtime"
        except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state — the same fallback the SDK runtime uses, see runtimes/claude_session.py::_project_runtime_root)
            root = None
    return _runner.state_dir_for(config.name, root=root)


class TuiSessionRuntime(
    StartupPromptInjectorMixin,
    TurnBridgeSeamMixin,
    TuiBootDrainMixin,
    RuntimeBase,
):
    """Interactive tmux-backed Claude TUI runtime (``spec.runtime: tui``).

    Delegates multiplexer mechanics to ``TmuxManager``; owns the session-name
    convention + the ``RuntimeBase`` surface. Tests inject an alternative
    ``multiplexer`` (any ``MultiplexerProtocol``) so the suite runs without tmux.
    """

    def __init__(
        self,
        multiplexer: Any | None = None,
        claude_bin: str = _CLAUDE_BIN_DEFAULT,
        command_builder: Any | None = None,
        turn_bridge_start: Any | None = None,
        turn_bridge_stop: Any | None = None,
    ) -> None:
        # Injection seams (tests pass in-memory fakes — real Protocol impls,
        # no mocks): ``multiplexer`` (MultiplexerProtocol; default TmuxManager),
        # ``command_builder`` ((config) -> apptainer-exec argv | None; default
        # :meth:`_default_argv`), and the A2A ``turn_bridge_start/stop`` seams
        # (None → resolve the real launcher lazily to avoid an import cycle).
        self._mux = multiplexer if multiplexer is not None else TmuxManager
        self._claude_bin = claude_bin
        self._command_builder = command_builder or self._default_argv
        self._turn_bridge_start = turn_bridge_start
        self._turn_bridge_stop = turn_bridge_stop

    def _default_argv(self, config: AgentConfig) -> list[str] | None:
        """Resolve the SIF and render the ``apptainer exec ... claude`` argv
        (``tui=True``) — the production launch command. Returns ``None`` when no
        SIF resolves (:meth:`start` turns that into a fail-loud error). Reuses
        ``ApptainerContainerRuntime`` so SIF resolution stays SDK-identical.
        """
        from ._apptainer_runtime import ApptainerContainerRuntime

        container_rt = ApptainerContainerRuntime()
        sif_path = container_rt.resolve_sif(config)
        if sif_path is None:
            return None
        state_dir = state_dir_for_config(config)
        return build_run_argv(config, state_dir=state_dir, sif_path=sif_path, tui=True)

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        """Materialise per-agent ``to_home/`` + CLAUDE.md into the container
        ``$HOME`` and return the host-side ``<state>/home/`` path.

        Thin delegator to :func:`_tui_workspace.materialize_workspace` (extracted
        to keep this module under the line limit). See that function's docstring
        for the per-step rationale (SDK-parity $HOME surface, settings.json USER
        scope, overlay upper-home, onboarding pre-seed).
        """
        return _materialize_workspace(config, state_dir_for_config=state_dir_for_config)

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
        one_shot: bool = False,
        drain_pickers_at_boot: bool = True,
        inject_startup_prompts: bool = True,
        boot_drain_timeout_s: float = 30.0,
    ) -> bool:
        """Launch the interactive ``claude`` TUI **inside apptainer**, held open
        by a detached tmux session sac owns.

        Parity with ``ClaudeSessionRuntime``: same SIF isolation / binds /
        overlay / auth / to_home — assembled by ``build_run_argv(tui=True)``.
        tmux wraps ``apptainer exec`` only to give the TUI a PTY and survive
        detach; it never runs ``claude`` on the host. ``force=True`` stops an
        existing session first; ``dry_run=True`` materialises the workspace +
        writes the argv to ``<state>/apptainer_run.argv.txt`` but skips
        ``tmux new-session``. ``foreground`` / ``no_preflight`` / ``one_shot``
        are accepted for RuntimeBase parity (no-ops for a detached session).

        Returns ``True`` only when the session is alive AND the boot-drain
        reached ready (BUG 3 — no false success); otherwise logs LOUD and
        returns ``False`` so ``agent_start`` surfaces the real boot failure.
        """
        del no_preflight, foreground, one_shot
        name = session_name_for(config)
        # Duplicate-session guard: no-op idempotently (outside --force/--dry-run)
        # instead of relaunching over a live session, and say so LOUDLY.
        if self._mux.exists(name) and not force:
            from ..cli_pkg._helpers._console import system_msg

            system_msg(
                f"duplicate session '{name}' — agent already running. "
                f"Attach: `sac agents attach {config.name}` "
                f"(or `tmux attach -t {name}`). "
                # NOT `sac agents restart` — when this guard fires DURING a
                # restart (the old session survived SIGTERM), that is the very
                # command that just failed, so recommending it loops the
                # operator back into the failure. Give the remedy that
                # actually works: kill the stale session, then start fresh.
                f"Force-relaunch: `tmux kill-session -t {name}` then "
                f"`sac agents start {config.name} -y --fresh`.",
                style="red",
            )
            if not dry_run:
                return True
        if force and self._mux.exists(name):
            self._mux.stop(name)
        self.materialize_workspace(config)

        from .._lifecycle._runtime_select import (
            warn_if_legacy_apptainer_runtime,
            warn_if_legacy_harness_key,
        )

        warn_if_legacy_apptainer_runtime(config)
        warn_if_legacy_harness_key(config)

        # Render the ``apptainer exec ... claude`` argv via the injection
        # seam (default :meth:`_default_argv` resolves the SIF + calls
        # build_run_argv(tui=True); tests inject a deterministic fake).
        argv = self._command_builder(config)
        if argv is None:
            import shutil as _shutil

            if _shutil.which("apptainer") is None and not dry_run:
                raise TuiAuthStageError(
                    "apptainer binary not found on $PATH — the TUI runtime "
                    "runs claude INSIDE apptainer (parity with the SDK "
                    "runtime). Install apptainer, or run on a host that has "
                    "it. (Legacy host-tmux TUI is retired.)"
                )
            return False

        if dry_run:
            from ._apptainer_argv_record import write_redacted_argv

            state_dir = state_dir_for_config(config)
            state_dir.mkdir(parents=True, exist_ok=True)
            write_redacted_argv(state_dir / "apptainer_run.argv.txt", argv)
            return True

        # LAUNCH GATE — the /uvwork scratch bind (ADR-0024), the overlay-venv
        # reconcile and the entry-point probe. All three are launch-time acts
        # that write to the host or refuse to start, so all three live past the
        # dry-run return and past the duplicate-session guard above, never
        # inside ``build_run_argv`` (which ``sac agents explain`` also calls).
        # Order and derivation rationale: :mod:`._tui_launch_gate`.
        from ._tui_launch_gate import run_launch_gate

        run_launch_gate(config, argv, state_dir=state_dir_for_config(config))

        # The host workdir is only the tmux launch cwd — the agent's real cwd is
        # ``--pwd`` inside the SIF; no session HOME/env (the container sets its own).
        workdir = (
            getattr(config, "expanded_workdir", "")
            or getattr(config, "workdir", "")
            or "/tmp"
        )
        # Redirect the inner ``apptainer exec … claude`` STDERR (apptainer FATAL
        # mount errors / an immediate claude exit) to a DURABLE per-agent log so
        # ``agent_start`` surfaces the real boot failure instead of a cause-less
        # ``<empty>`` pane tail. ``2>`` truncates per start.
        boot_stderr_log = state_dir_for_config(config) / "boot.stderr.log"
        command = " ".join(shlex.quote(a) for a in argv) + (
            f" 2> {shlex.quote(str(boot_stderr_log))}"
        )
        started = bool(
            self._mux.start(
                session_name=name,
                command=command,
                workdir=str(workdir),
                session_env={"CLAUDE_DISABLE_AUTO_UPDATE": "1"},
            )
        )
        # BUG 3 (false success): whether the boot-drain observed a ready
        # signal. Only meaningful when the drain ran; ``None`` means "not
        # verified this boot" (drain disabled) → we fall back to a liveness
        # check at the end rather than blindly claiming success.
        reached_ready: bool | None = None
        if started and drain_pickers_at_boot:
            # Drain the picker registry AT BOOT so the supervisor + downstream
            # send paths land on a TUI already at the input field (the
            # supervisor never calls send_turn during boot). Adaptive window:
            # ``startup_commands`` can delay ``exec claude`` by minutes, so
            # stretch the drain to cover the bootstrap (it returns as soon as
            # claude is up — see ``_drain_at_boot``).
            effective_timeout = boot_drain_timeout_s
            if list(getattr(config, "startup_commands", []) or []):
                effective_timeout = max(boot_drain_timeout_s, _STARTUP_BOOT_DRAIN_S)
            reached_ready = self._drain_at_boot(config, timeout_s=effective_timeout)
        if started and inject_startup_prompts:
            # Boot-mission injection — parity with the SDK runtime: feed
            # spec.startup_prompts as the first turn(s). Best-effort.
            self._inject_startup_prompts(config)
        if started:
            # A2A wake-on-push parity: give the TUI the same ``/v1/turn``
            # endpoint the SDK runner serves. Best-effort — a failed bridge
            # must not fail the start.
            self._maybe_start_turn_bridge(config)
        # BUG 3 (false success — constitution §2 "no surprises / fail loud"):
        # up to here ``started`` only proves ``tmux new-session`` succeeded, NOT
        # that the inner claude survived boot and reached its input-ready state.
        # A continue-mode agent whose session DIED (e.g. an Escape cancelled the
        # dev-channels modal) still had ``started=True`` — so agent_start printed
        # "restarted" over a corpse. Now verify: session alive AND (drain saw
        # ready, or — when the drain was disabled — a liveness probe agrees). On
        # failure return False so ``agent_start`` raises its LOUD diagnostic
        # (pane tail + boot.stderr + `tmux attach` hint) and no success line is
        # printed.
        if started and not dry_run:
            alive = self._mux.exists(name)
            ok = start_succeeded(
                session_alive=alive,
                reached_ready=reached_ready,
                is_running=self.is_running(config) if alive else False,
            )
            if not ok:
                import logging

                logging.getLogger(__name__).error(
                    "TuiSessionRuntime: start FAILED for %s — tmux session "
                    "%s (session_alive=%s, reached_ready=%s). The inner claude "
                    "did not survive boot and reach its input field. Reproduce "
                    "live: `tmux attach -t %s`. (See the boot-drain errors above "
                    "and <state>/boot.stderr.log for the cause.)",
                    getattr(config, "name", "?"),
                    "is GONE" if not alive else "is alive but never signalled ready",
                    alive,
                    reached_ready,
                    name,
                )
                return False
        return started

    def stop(self, config: AgentConfig) -> bool:
        """Kill the tmux session sac owns for this agent.

        Returns True iff a session existed AND was terminated (no-op on absent
        session, so the supervisor's ``stop()->start()`` cycle stays idempotent).
        """
        # Tear down the A2A turn bridge first so it stops accepting wake POSTs
        # before the tmux session it injects into goes away.
        self._maybe_stop_turn_bridge(config)
        name = session_name_for(config)
        return bool(self._mux.stop(name))

    def is_running(
        self, config: AgentConfig, max_idle_s: float = _DEFAULT_MAX_IDLE_S
    ) -> bool:
        """LIVENESS: ``tui-<name>`` exists AND its pane process is alive
        (``os.kill(pane_pid, 0)``; NO activity gate — an idle agent is
        still running). ``max_idle_s`` ignored. See :mod:`_tui_liveness`."""
        del max_idle_s
        return pane_process_alive(
            session_name_for(config),
            exists_fn=self._mux.exists,
            pane_dead_fn=getattr(self._mux, "pane_dead", None),
            pane_pid_fn=getattr(self._mux, "pane_pid", None),
        )

    def agent_pid(self, config: AgentConfig) -> int | None:
        """The pane's long-lived ``apptainer exec ... claude`` pid.

        The ``RuntimeBase`` seam that hands ``instances.pid`` its value.
        This is the SAME signal :meth:`is_running` above keys its verdict
        on (both go through the pane pid), so the registry and
        ``is_running`` cannot disagree about which process is this agent.

        NOT the launcher pid: the launcher spawns the tmux session and
        returns within seconds. The pane's ``bash -c`` ``exec``s
        apptainer — ``exec`` keeps the pid — so the pane pid IS the
        long-lived container process. See :func:`_tui_liveness.pane_pid_of`.
        """
        return pane_pid_of(
            session_name_for(config),
            pane_pid_fn=getattr(self._mux, "pane_pid", None),
        )

    def session_name(self, config: AgentConfig) -> str | None:
        """The ``tui-<name>`` session — the seam ``instances.screen`` reads.

        THE SAME call :meth:`start` passes to ``tmux new-session -s``.
        """
        return session_name_for(config)

    def is_responsive(
        self, config: AgentConfig, max_idle_s: float = _DEFAULT_MAX_IDLE_S
    ) -> bool:
        """RESPONSIVENESS: alive AND pane activity within ``max_idle_s``
        (the OLD is_running rule, for hang-detection). See
        :mod:`_tui_liveness`."""
        name = session_name_for(config)
        if not self._mux.exists(name):
            return False
        return is_responsive_from_activity(
            self._mux.session_activity(name), time.time(), max_idle_s
        )

    def send_turn(
        self,
        config: AgentConfig,
        text: str,
        *,
        wait_ready: bool = True,
    ) -> bool:
        """Deliver one turn to the in-tmux TUI; ``False`` when NOT delivered.

        False covers three distinct cases a caller must tell apart: no tmux
        session (nothing to deliver to), and — since 2026-08-18 — a pane that
        would PARK the turn rather than run it, either mid-turn or already
        holding queued input. See :mod:`runtimes._tui_delivery` for the
        mechanism and the measurement, and :mod:`runtimes._pane_acceptance`
        for why "not busy" is not the test.
        """
        return _tui_delivery.send_turn_to_pane(
            self._mux,
            session_name_for(config),
            text,
            wait_ready=wait_ready,
            ensure_ready=lambda: self.wait_until_input_ready(config),
        )

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Return the last ``lines`` of pane output; empty string when the
        session does not exist (distinguish via ``is_running`` first).
        """
        return _tui_delivery.capture_pane_logs(
            self._mux, session_name_for(config), lines
        )

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        """Reason a turn would not be delivered now, or None if it would be.

        So a caller's error can name the ACTUAL cause instead of guessing one.
        """
        return _tui_delivery.why_not_deliverable(self._mux, session_name_for(config))
