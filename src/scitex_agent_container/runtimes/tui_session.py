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
from ._tui_session_start import TuiSessionStartMixin
from ._tui_session_support import (
    _CLAUDE_BIN_DEFAULT,
    _DEFAULT_MAX_IDLE_S,
    TuiStopVerificationError,
    candidate_session_names_for,
    legacy_session_name_for,
    resolve_live_session_for,
    session_name_for,
    start_succeeded,
    state_dir_for_config,
)
from ._tui_workspace import materialize_workspace as _materialize_workspace
from .base import RuntimeBase

__all__ = [
    "TuiAuthStageError",
    "TuiInputNotReadyError",
    "TuiSessionRuntime",
    "TuiStopVerificationError",
    "_compose_pending_live",
    "candidate_session_names_for",
    "clear_compose_buffer",
    "drain_modals_until_ready",
    "legacy_session_name_for",
    "resolve_live_session_for",
    "session_name_for",
    "start_succeeded",
    "state_dir_for_config",
    "verify_submit_by_advancement",
]


class TuiSessionRuntime(
    TuiSessionStartMixin,
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
        inbox_dispatcher_start: Any | None = None,
        inbox_dispatcher_stop: Any | None = None,
        cct_poller_start: Any | None = None,
        cct_poller_stop: Any | None = None,
    ) -> None:
        # Injection seams (tests pass in-memory fakes — real Protocol impls,
        # no mocks): ``multiplexer`` (MultiplexerProtocol; default TmuxManager),
        # ``command_builder`` ((config) -> apptainer-exec argv | None; default
        # :meth:`_default_argv`), and the A2A ``turn_bridge_start/stop`` seams
        # (None → resolve the real launcher lazily to avoid an import cycle).
        self._mux = multiplexer if multiplexer is not None else TmuxManager
        self._claude_bin = claude_bin
        self._production_command_builder = command_builder is None
        self._command_builder = command_builder or self._default_argv
        self._turn_bridge_start = turn_bridge_start
        self._turn_bridge_stop = turn_bridge_stop
        self._inbox_dispatcher_start = inbox_dispatcher_start
        self._inbox_dispatcher_stop = inbox_dispatcher_stop
        self._cct_poller_start = cct_poller_start
        self._cct_poller_stop = cct_poller_stop

    @staticmethod
    def _owns_external_cct_poller(config: AgentConfig) -> bool:
        """Whether this base runtime owns CCT for the selected TUI harness.

        Hermes keeps its established subclass lifecycle.  Codex uses this
        harness-neutral base lifecycle so inbound transport is tied to the
        exact SAC-managed ``/v1/turn`` session rather than an MCP launch.
        """
        return str(getattr(config, "harness", "") or "").lower() == "codex"

    def _start_cct_poller(self, config: AgentConfig) -> None:
        start = self._cct_poller_start
        if start is None:
            from ._tui_cct_poller import start_cct_poller as start

        start(config)

    def _stop_cct_poller(self, config: AgentConfig) -> None:
        stop = self._cct_poller_stop
        if stop is None:
            from ._tui_cct_poller import stop_cct_poller as stop

        stop(config)

    def _start_inbox(self, config: AgentConfig) -> None:
        """Start the harness-neutral durable inbox dispatcher."""
        start = self._inbox_dispatcher_start
        if start is None:
            from ._channel_inbox_dispatcher_lifecycle import (
                start_inbox_dispatcher as start,
            )

        start(config)

    def _stop_inbox(self, config: AgentConfig) -> None:
        """Stop the exact dispatcher owned by this agent incarnation."""
        stop = self._inbox_dispatcher_stop
        if stop is None:
            from ._channel_inbox_dispatcher_lifecycle import (
                stop_inbox_dispatcher as stop,
            )

        stop(config)

    def _live_session_name(self, config: AgentConfig) -> str | None:
        """The LIVE session to address, or ``None`` when neither is up.

        Bare canonical preferred, legacy ``tui-`` recognised. ``None`` lets
        callers skip the read without a second existence probe — resolution
        already proved absence.
        """
        for candidate in candidate_session_names_for(config):
            try:
                if self._mux.exists(candidate):
                    return candidate
            except Exception:  # stx-allow: fallback (a failed exists-probe is blindness on that candidate, not absence — keep trying the next one)
                continue
        return None

    def _session_name_or_canonical(self, config: AgentConfig) -> str:
        """Live session when one is up, else the canonical bare name.

        The minting default for paths that must hand a name downstream even
        when nothing is live (the downstream read then reports the miss).
        """
        return self._live_session_name(config) or session_name_for(config)

    def send_key(self, config: AgentConfig, key: str) -> bool:
        """Deliver one bounded, explicit control key to the owned TUI pane."""
        normalized = {
            "ESC": "Escape",
            "Escape": "Escape",
            "Enter": "Enter",
            "C-c": "C-c",
            "SIGINT": "C-c",
        }.get(key)
        if normalized is None:
            raise ValueError(f"unsupported TUI control key: {key!r}")
        name = self._live_session_name(config)
        if not name:
            return False
        self._mux.send_keys(name, normalized)
        return True

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
        self._resolved_sif_path = sif_path
        state_dir = state_dir_for_config(config)
        argv = build_run_argv(config, state_dir=state_dir, sif_path=sif_path, tui=True)
        if getattr(config, "harness", "") == "hermes":
            from ._hermes_profile import validate_hermes_tui_profile

            validate_hermes_tui_profile(config, state_dir=state_dir, launch_argv=argv)
        if getattr(config, "harness", "") == "opencode":
            from ._opencode_profile import validate_opencode_profile

            validate_opencode_profile(config, state_dir=state_dir, launch_argv=argv)
        return argv

    def resolved_image_identity(self) -> dict[str, str] | None:
        """Exact immutable artifact selected for this runtime's last launch."""
        path = getattr(self, "_resolved_sif_path", None)
        if path is None:
            return None
        from ._apptainer_image_ref import image_artifact_identity

        return image_artifact_identity(path)

    def resolved_storage_identity(self) -> dict[str, str] | None:
        """Exact write-heavy paths selected for this runtime's last launch."""
        value = getattr(self, "_resolved_launch_storage", None)
        return dict(value) if value is not None else None

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        """Materialise per-agent ``to_home/`` + CLAUDE.md into the container
        ``$HOME`` and return the host-side ``<state>/home/`` path.

        Thin delegator to :func:`_tui_workspace.materialize_workspace` (extracted
        to keep this module under the line limit). See that function's docstring
        for the per-step rationale (SDK-parity $HOME surface, settings.json USER
        scope, overlay upper-home, onboarding pre-seed).
        """
        home = _materialize_workspace(config, state_dir_for_config=state_dir_for_config)
        if home is not None and getattr(config, "harness", "") == "hermes":
            from ._hermes_profile import materialize_hermes_tui_profile

            materialize_hermes_tui_profile(
                config,
                state_dir=state_dir_for_config(config),
                deploy_home=False,
            )
        return home

    def stop(self, config: AgentConfig) -> bool:
        """Kill the tmux session(s) sac owns for this agent.

        Stops EVERY live candidate — bare canonical AND legacy ``tui-`` — so
        a cutover-straddling agent can never be left half-running under its
        old name. Returns True iff a session existed AND was terminated
        (no-op on absent sessions, so the supervisor's ``stop()->start()``
        cycle stays idempotent).
        """
        # Sealed run: record that SAC asked for this stop BEFORE the pane
        # teardown, so the outcome attributes it whatever exits first.
        from .._sandbox import is_sandboxed

        if is_sandboxed(config):
            from .._sandbox._run import mark_stop_requested

            mark_stop_requested(state_dir_for_config(config))
        # Stop inbound owners before the A2A sink and pane disappear.
        if self._owns_external_cct_poller(config):
            self._stop_cct_poller(config)
        if str(getattr(config, "harness", "") or "").lower() != "hermes":
            self._stop_inbox(config)
        self._maybe_stop_turn_bridge(config)
        from .._sandbox import end_run

        try:
            stopped_any = self._stop_panes(config)
        except Exception:
            # A sealed run's outcome is finalized EVEN when the pane teardown
            # check fails or is too slow under load; its layers stay until a
            # later stop/start verifies the container is gone.
            if is_sandboxed(config):
                end_run(config, state_dir_for_config(config), teardown_verified=False)
            raise
        # Sealed run: outcome (usage, files) + run.after once the pane is gone.
        if is_sandboxed(config):
            end_run(config, state_dir_for_config(config))
        return stopped_any

    def _stop_panes(self, config: AgentConfig) -> bool:
        """Tear down every live candidate session; raise if one survives."""
        stopped_any = False
        for name in candidate_session_names_for(config):
            if not self._mux.exists(name):
                continue
            if not self._mux.stop(name):
                raise TuiStopVerificationError(
                    f"TUI stop for {config.name!r} could not verify that both "
                    f"tmux session {name!r} and its owned process cgroup are gone"
                )
            stopped_any = True
        return stopped_any

    def is_running(
        self, config: AgentConfig, max_idle_s: float = _DEFAULT_MAX_IDLE_S
    ) -> bool:
        """LIVENESS: the live session exists AND its pane process is alive.

        Reads the LIVE session — bare canonical first, legacy ``tui-<name>``
        second — so a pre-cutover session still counts as running until it is
        restarted under the new name. ``os.kill(pane_pid, 0)``; NO activity
        gate — an idle agent is still running. ``max_idle_s`` ignored. See
        :mod:`_tui_liveness`."""
        del max_idle_s
        live = self._live_session_name(config)
        if live is None:
            return False
        return pane_process_alive(
            live,
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
            self._session_name_or_canonical(config),
            pane_pid_fn=getattr(self._mux, "pane_pid", None),
        )

    def session_name(self, config: AgentConfig) -> str | None:
        """The LIVE session for this agent — the seam ``instances.screen`` reads.

        Bare canonical when live, else legacy ``tui-<name>`` when THAT is the
        live one, else the canonical bare name (the minting default). THE SAME
        bare name :meth:`start` passes to ``tmux new-session -s``.
        """
        return self._session_name_or_canonical(config)

    def is_responsive(
        self, config: AgentConfig, max_idle_s: float = _DEFAULT_MAX_IDLE_S
    ) -> bool:
        """RESPONSIVENESS: alive AND pane activity within ``max_idle_s``
        (the OLD is_running rule, for hang-detection). See
        :mod:`_tui_liveness`."""
        name = self._live_session_name(config)
        if name is None:
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
            self._session_name_or_canonical(config),
            text,
            wait_ready=wait_ready,
            ensure_ready=lambda: self.wait_until_input_ready(config),
        )

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Return the last ``lines`` of pane output; empty string when the
        session does not exist (distinguish via ``is_running`` first).
        """
        return _tui_delivery.capture_pane_logs(
            self._mux, self._session_name_or_canonical(config), lines
        )

    def why_not_deliverable(self, config: AgentConfig) -> str | None:
        """Reason a turn would not be delivered now, or None if it would be.

        So a caller's error can name the ACTUAL cause instead of guessing one.
        """
        return _tui_delivery.why_not_deliverable(
            self._mux, self._session_name_or_canonical(config)
        )
