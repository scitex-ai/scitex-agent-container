"""``TuiSessionRuntime.start`` — split out of ``tui_session.py``.

Moved verbatim (no behaviour change) because ``tui_session.py`` was over the
line limit. The method still runs as ``TuiSessionRuntime.start`` through
:class:`TuiSessionStartMixin`; it logs under the ``tui_session`` logger name
it always used.
"""

from __future__ import annotations

import shlex

from ..config import AgentConfig
from ._tui_auth_stage import TuiAuthStageError
from ._tui_session_support import (
    _STARTUP_BOOT_DRAIN_S,
    candidate_session_names_for,
    session_name_for,
    start_succeeded,
    state_dir_for_config,
)

__all__ = ["TuiSessionStartMixin"]

#: The logger name ``start`` used before the split.
_LOGGER_NAME = "scitex_agent_container.runtimes.tui_session"


class TuiSessionStartMixin:
    """Provides :meth:`start` for :class:`.tui_session.TuiSessionRuntime`."""

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
        live_now = [c for c in candidate_session_names_for(config) if self._mux.exists(c)]
        # Duplicate-session guard: no-op idempotently (outside --force/--dry-run)
        # instead of relaunching over a live session, and say so LOUDLY.
        # BOTH names are checked: a legacy ``tui-<name>`` session from before
        # the cutover is just as much "already running" as a bare one, and
        # launching a bare session over it would leave two live TUIs for one
        # agent with the router talking to only one of them.
        if live_now and not force:
            from ..cli_pkg._helpers._console import system_msg

            system_msg(
                f"duplicate session '{live_now[0]}' — agent already running. "
                f"Attach: `sac agents attach {config.name}` "
                f"(or `tmux attach -t {live_now[0]}`). "
                # NOT `sac agents restart` — when this guard fires DURING a
                # restart (the old session survived SIGTERM), that is the very
                # command that just failed, so recommending it loops the
                # operator back into the failure. Give the remedy that
                # actually works: kill the stale session, then start fresh.
                f"Force-relaunch: `tmux kill-session -t {live_now[0]}` then "
                f"`sac agents start {config.name} -y --fresh`.",
                style="red",
            )
            if not dry_run:
                return True
        if not dry_run:
            from ._native_tui_admission import preflight_native_tui

            preflight_native_tui(
                config, production=self._production_command_builder
            )
        if force:
            for stale in live_now:
                self._mux.stop(stale)
        if not dry_run:
            # Sealed run (spec.sandbox): a FRESH run dir + $HOME before the
            # workspace materialises into it (no-op when not sandboxed).
            from .._sandbox import begin_run

            begin_run(config, state_dir_for_config(config))
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
        from ._launch_storage import launch_storage_identity

        self._resolved_launch_storage = launch_storage_identity(argv)

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
        # ``<empty>`` pane tail. Host-side truncation occurs before tmux starts;
        # TmuxManager installs the stderr redirect before its first shell step,
        # so even a failed ``cd`` or venv activation survives the pane's exit.
        boot_stderr_log = state_dir_for_config(config) / "boot.stderr.log"
        boot_stderr_log.parent.mkdir(parents=True, exist_ok=True)
        boot_stderr_log.write_text("")
        boot_stderr_log.chmod(0o600)
        launch_argv = argv
        from .._sandbox import is_sandboxed, supervised_argv

        if is_sandboxed(config):
            image = getattr(self, "_resolved_sif_path", None)
            if image is None:
                raise TuiAuthStageError(
                    f"sealed run {config.name!r}: no resolved SIF to record"
                )
            launch_argv = supervised_argv(
                config, argv, state_dir=state_dir_for_config(config), image=str(image)
            )
        command = " ".join(shlex.quote(a) for a in launch_argv)
        started = bool(
            self._mux.start(
                session_name=name,
                command=command,
                workdir=str(workdir),
                session_env={
                    "CLAUDE_DISABLE_AUTO_UPDATE": "1",
                    "SAC_TMUX_BOOT_STDERR_PATH": str(boot_stderr_log),
                    "SAC_TMUX_START_DIAGNOSTICS_PATH": str(
                        state_dir_for_config(config) / "tmux.start.json"
                    ),
                },
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
            # One host daemon owns the durable SAC/Cards subscriptions for
            # every TUI harness.  It hands neutral envelopes to /v1/turn;
            # that endpoint's runtime adapter decides Claude Code, Hermes, or
            # Codex delivery.  Starting it after the turn bridge prevents an
            # inbox consumer from acknowledging work before a sink exists.
            if str(getattr(config, "harness", "") or "").lower() != "hermes":
                self._start_inbox(config)
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
                import scitex_logging as slogging

                slogging.getLogger(_LOGGER_NAME).error(
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
            if self._owns_external_cct_poller(config):
                try:
                    self._start_cct_poller(config)
                except Exception:
                    # CCT is part of the requested managed session contract.
                    # Never leave a TUI advertised as live after its sole
                    # inbound owner failed to start, and never fall back to an
                    # MCP-owned second poller.
                    self._stop_cct_poller(config)
                    self._stop_inbox(config)
                    self._maybe_stop_turn_bridge(config)
                    self._mux.stop(name)
                    raise
            # Hermes autonomy is armed by its detached monitor after that
            # monitor positively observes an idle footer and empty composer.
            # Never stage SAC control text over the initial model turn here.
        return started
