"""``tui`` runtime adapter — June-15 SDK-pool-cutoff TUI pivot (hedge).

Operator directive 12861 (lead a2a ``e39ab77a181340b6975b6ad7aea70329``):
from June 15 the SDK / ``claude -p`` path becomes increasingly
disadvantageous for subscription users (the Max-pool cutoff —
``project_sdk_subscription_cutoff_tui_pivot``). The operator wants
the fleet to gradually shift weight to TUI mode while keeping sac
as the always-launcher (single SSoT = spec.yaml).

``spec.runtime: tui`` selects this adapter. The bundled ``claude`` CLI
runs interactively inside a tmux session that sac owns; sac wires
the agent's MCP, healthchecks the TUI's responsiveness, and restarts
the inner process on crash — same lifecycle contract as
``ClaudeSessionRuntime`` for ``runtime: claude-agent-sdk``.

Cherry-picked Day-1 substance from parked PR #353 (``feat/tmux-runner-day1-salvage``):
the ``_runners/_tmux/`` modules (multiplexer / tmux / pane_capture /
prompts / auto) are the primitives this runtime delegates to. The
adapter is intentionally a thin shim — it owns the session-name
convention and the ``RuntimeBase`` surface; the tmux mechanics
themselves live in their salvaged home so screen / future-other
multiplexer adapters can share them.

The four TUI-stability risks I named in the design A2A (lead a2a
``c8edc13babb44852a8f547b1398e200b``):

  1. **Session-detach survival** — HANDLED. ``TmuxManager.start``
     spawns a detached tmux session (``new-session -d``); the agent
     survives operator detach + terminal-emulator restart.
  2. **Health-check signal** — DEFERRED to follow-up PR. ``is_running``
     today only proves the tmux session exists, not that the inner
     ``claude`` process is responsive. Follow-up wires a tui-alive
     probe (pane-activity timestamp or a sidecar heartbeat file).
  3. **Restart-on-crash semantics** — PARTIALLY HANDLED. The
     RestartPolicy + this runtime's ``is_running`` lets the
     supervisor detect a dead tmux session; INNER-process death
     while the tmux session is still alive is the deferred half
     (paired with risk 2).
  4. **Memory pressure under long-running TUI** — DEFERRED. Requires
     ``spec.context_management: auto-compact`` parity inside the TUI
     runner; tracked in a follow-up so this hedge ships before the
     2026-06-15 cutoff.

The hedge is INTENTIONALLY behind ``spec.runtime: tui`` (operator
opt-in per agent); the default ``"claude-agent-sdk"`` path is
untouched. No live cutover is implied — operator decides when to
flip a specific agent.

Cross-thread context: the hub Workspace-Console SIF rebuild (task
#11, hub card ``sif-claude-sdk-bundle``) ships the SDK-bundled
``claude`` CLI as ``/usr/local/bin/claude`` in the apptainer image
— the SAME binary this runner exec's. Lands in parallel; the TUI
runner picks up whatever ``claude`` version the rebuilt SIF provides.
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
from . import prompts as _prompts
from ._apptainer_build_argv import build_run_argv
from ._to_home import deploy_to_home
from ._to_home_overlay import deploy_to_home_overlay, resolve_overlay_upper_home
from ._tui_auth_stage import TuiAuthStageError
from .base import RuntimeBase
from .claude_md import setup_claude_md
from .onboarding import ensure_project_onboarding

__all__ = [
    "TuiAuthStageError",
    "TuiInputNotReadyError",
    "TuiSessionRuntime",
    "session_name_for",
    "state_dir_for_config",
]


_CLAUDE_BIN_DEFAULT = "claude"

_REQUIRED_CONFIG_ATTRS = ("expanded_workdir", "skills", "claude", "env", "labels")

# Default max-idle window for the tui-alive probe (see
# ``TuiSessionRuntime.is_running``). 300s mirrors the SDK runtime's
# health.interval default and gives a quiet but healthy TUI a
# generous grace window before the supervisor's restart policy
# fires on it. Overridable per-call via the ``max_idle_s`` kwarg
# so a custom health policy in spec.health can tune it without a
# code change.
_DEFAULT_MAX_IDLE_S = 300.0

# Boot-drain window when ``spec.startup_commands`` delay ``exec claude``
# (e.g. an in-container ``uv pip install`` that runs for minutes before
# the TUI launches). The drain polls through the bootstrap and dismisses
# claude's first-run modals (bypass-permissions / trust / theme) the
# moment they appear; it returns as soon as claude is up, so this is a
# CAP, not a fixed block. 240s comfortably covers a cold uv resolve.
_STARTUP_BOOT_DRAIN_S = 240.0


def session_name_for(config: AgentConfig) -> str:
    """Return the tmux session name owned by sac for this agent.

    ``tui-<agent-name>`` namespace-prefixes the session so it cannot
    collide with operator-owned tmux sessions on the same host;
    ``TmuxManager.exists`` keys off the same string so the runtime
    can probe its own session deterministically across processes.
    """
    return f"tui-{config.name}"


def state_dir_for_config(config: AgentConfig) -> Path:
    """Per-agent state dir on the host: project-local if the agent's
    spec lives under a project-scope ``.scitex/agent-container/`` tree
    (a git repo with that subdir), else
    ``~/.scitex/agent-container/runtime/<name>/``.

    Mirrors ``ClaudeSessionRuntime._state_dir`` so the TUI runtime
    lands per-agent files in the same place the SDK runtime would —
    the operator's ``sac agents status`` / ``sac agents prune-claude``
    / external tooling can address ``<state>/`` uniformly regardless
    of which runtime is selected.
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


class TuiSessionRuntime(RuntimeBase):
    """Interactive tmux-backed Claude TUI runtime.

    Selected by ``spec.runtime: tui``. Delegates all multiplexer
    mechanics to ``TmuxManager`` (salvaged module — see the
    cherry-pick note in the module docstring); the adapter owns
    only the session-name convention and the ``RuntimeBase`` surface.

    Tests inject an alternative ``multiplexer`` (any class satisfying
    ``MultiplexerProtocol``) so the suite runs without requiring tmux
    in the CI container.
    """

    def __init__(
        self,
        multiplexer: Any | None = None,
        claude_bin: str = _CLAUDE_BIN_DEFAULT,
        command_builder: Any | None = None,
        turn_bridge_start: Any | None = None,
        turn_bridge_stop: Any | None = None,
    ) -> None:
        # Default to the real TmuxManager; tests pass an in-memory
        # MultiplexerProtocol fake. No mocks — the fake is a real
        # implementation of the same Protocol the real TmuxManager
        # satisfies, just backed by a dict instead of subprocess.
        self._mux = multiplexer if multiplexer is not None else TmuxManager
        self._claude_bin = claude_bin
        # Injection seam (mirrors ``multiplexer`` + ClaudeSessionRuntime's
        # ``container_runtime_for``): ``(config) -> list[str] | None`` —
        # the ``apptainer exec ... claude`` argv to launch in tmux, or
        # ``None`` when no SIF could be resolved. Tests inject a fake that
        # returns a deterministic argv so the tmux-dispatch glue runs
        # without a real apptainer/SIF. Default = :meth:`_default_argv`.
        self._command_builder = command_builder or self._default_argv
        # Injection seams for the A2A turn bridge — ``(config) -> Any``.
        # The bridge gives a TUI agent the same ``/v1/turn`` endpoint the
        # SDK runner serves, so a bus-pushed message WAKES the idle TUI
        # (see :mod:`_tui_turn_bridge`). ``None`` → resolve the real
        # launcher lazily at call time (avoids an import cycle:
        # ``_tui_turn_bridge`` imports this module). Tests inject recording
        # fakes so start()/stop() never spawn a real subprocess.
        self._turn_bridge_start = turn_bridge_start
        self._turn_bridge_stop = turn_bridge_stop

    def _default_argv(self, config: AgentConfig) -> list[str] | None:
        """Resolve the SIF and render the ``apptainer exec ... claude``
        argv (``tui=True``) — the production launch command.

        Returns ``None`` when no SIF resolves (e.g. apptainer absent);
        :meth:`start` turns that into a fail-loud error on a real run.
        Reuses ``ApptainerContainerRuntime`` so SIF resolution / build /
        the missing-apptainer diagnostic stay identical to the SDK path.
        """
        from ._apptainer_runtime import ApptainerContainerRuntime

        container_rt = ApptainerContainerRuntime()
        sif_path = container_rt.resolve_sif(config)
        if sif_path is None:
            return None
        state_dir = state_dir_for_config(config)
        return build_run_argv(config, state_dir=state_dir, sif_path=sif_path, tui=True)

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        """Materialise per-agent ``to_home/`` + CLAUDE.md into the
        container ``$HOME`` and return the host-side ``<state>/home/`` path.

        Mirrors ``ClaudeSessionRuntime._setup_workspace`` EXACTLY so the
        in-apptainer TUI gets the same $HOME surface as the SDK path:

          * ``setup_claude_md`` writes the sac-managed CLAUDE.md skill
            chain into ``<state>/home/CLAUDE.md``.
          * ``deploy_to_home`` overlays the shared ``_shared/to_home``
            baseline + per-agent ``to_home/`` (.mcp.json, .env,
            .claude/{hooks,skills,settings.json}) into ``<state>/home/``
            (the host dir bound at ``/home/agent``).
          * ``deploy_to_home_overlay`` mirrors the SAME tree into the
            overlay upper-home for relaxed ``--home``/``--overlay`` specs
            (where the workspace-home bind is shadowed). No-op otherwise.

        Credentials are NOT staged here: the in-apptainer TUI receives
        them via the writable file-bind ``spec.claude.credentials_file``
        (or the account/host dir-bind) emitted in ``build_run_argv`` —
        single source of truth, no copy to desync.

        ``ensure_project_onboarding`` pre-seeds the per-workspace entry
        in ``$HOME/.claude.json`` so the TUI skips the workspace-trust
        wizard; it is written into BOTH the workspace-home and (when
        present) the overlay upper-home so it lands regardless of which
        home-delivery mode the spec uses.

        Returns ``None`` for stub configs lacking the full AgentConfig
        surface (unit-test ``SimpleNamespace`` fixtures); the caller
        treats that as "skip materialise".
        """
        if not all(hasattr(config, a) for a in _REQUIRED_CONFIG_ATTRS):
            return None
        home_dir = state_dir_for_config(config) / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        setup_claude_md(config, str(home_dir))
        deploy_to_home(config, str(home_dir))
        # Relaxed ``--home``/directory-overlay specs shadow the
        # workspace-home bind; mirror the same to_home tree into the
        # overlay upper-home so it reaches the container $HOME. No-op for
        # non-overlay specs (the workspace-home bind suffices).
        deploy_to_home_overlay(config)
        workdir = (
            getattr(config, "expanded_workdir", "")
            or getattr(config, "workdir", "")
            or "/tmp"
        )
        ensure_project_onboarding(workdir, home=home_dir)
        upper_home = resolve_overlay_upper_home(config)
        if upper_home is not None and upper_home.is_dir():
            ensure_project_onboarding(workdir, home=upper_home)
        return home_dir

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
        drain_pickers_at_boot: bool = True,
        inject_startup_prompts: bool = True,
        boot_drain_timeout_s: float = 30.0,
    ) -> bool:
        """Launch the interactive ``claude`` TUI **inside apptainer**,
        held open by a detached tmux session sac owns.

        Parity with ``ClaudeSessionRuntime`` (the SDK runtime): the
        agent runs in the SIF with the SAME isolation, binds, overlay,
        auth, and to_home delivery — assembled by
        :func:`_apptainer_build_argv.build_run_argv` with ``tui=True``.
        The only differences from the SDK path are (1) the inner process
        is the interactive ``claude`` TUI instead of the ``python -m``
        session runner, and (2) tmux wraps the ``apptainer exec`` so the
        TUI gets a PTY and survives operator detach. tmux is a PTY
        holder only — it never runs ``claude`` on the host.

        ``force=True`` stops any existing session before starting
        (idempotent-restart contract). ``dry_run=True`` materialises the
        workspace + writes the rendered argv to
        ``<state>/apptainer_run.argv.txt`` but skips ``tmux new-session``.
        ``foreground`` / ``no_preflight`` are accepted for RuntimeBase
        parity; a detached-by-design tmux session has no foreground mode.
        """
        del no_preflight, foreground
        name = session_name_for(config)
        if force and self._mux.exists(name):
            self._mux.stop(name)
        self.materialize_workspace(config)

        from .._lifecycle._runtime_select import warn_if_legacy_apptainer_runtime

        warn_if_legacy_apptainer_runtime(config)

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
            state_dir = state_dir_for_config(config)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "apptainer_run.argv.txt").write_text("\n".join(argv) + "\n")
            return True

        # tmux runs ``exec <command>`` in a bash -c; pass the apptainer
        # argv shell-quoted so the PTY wraps ``apptainer exec ... claude``.
        # No HOME/CLAUDE_CONFIG_DIR session env: the container sets its
        # own $HOME (=/home/agent) and the host-side tmux shell's env is
        # irrelevant to the in-SIF process. The host workdir is only the
        # tmux launch cwd — the agent's real cwd is ``--pwd`` inside the SIF.
        workdir = (
            getattr(config, "expanded_workdir", "")
            or getattr(config, "workdir", "")
            or "/tmp"
        )
        # B->A feedback (fail-fast / fail-loud / no silent fallback): the inner
        # ``apptainer exec … claude`` renders its TUI to the tmux PANE (stdout),
        # but its STDERR — where apptainer's FATAL mount errors and an immediate
        # claude exit land — would otherwise die with the pane, leaving the
        # launcher only a cause-less "<empty>" pane tail. Redirect that stderr
        # to a DURABLE per-agent log from t=0 (no pipe-pane race) so
        # ``agent_start`` (``_read_boot_stderr_section``) surfaces the real boot
        # failure. ``2>`` truncates per start. No mkdir here: deploy_to_home
        # already created ``<state>/home`` (so ``<state>/`` exists), and keeping
        # this seam free of real-FS writes lets the unit suite record the
        # command via a fake mux without polluting ``~/.scitex``.
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
        if started and drain_pickers_at_boot:
            # URGENT (lead a2a 278159b5, 2026-06-14): drain the
            # picker registry AT BOOT so the supervisor + downstream
            # send paths land on a TUI already at the input field.
            # Wiring drain into send_turn alone was lazy — the
            # supervisor never calls send_turn during boot, so the
            # picker sat up and every `sac agents send` then timed
            # out. Failure here MUST NOT fail the start (supervisor
            # restart loop would oscillate); log + let the send_turn
            # retry on its own hook.
            #
            # Adaptive window (2026-06-15): when ``startup_commands``
            # delay ``exec claude`` (e.g. an in-container uv install of
            # minutes), a fixed 30s drain expires BEFORE claude renders
            # its first modal — the bypass-permissions / trust picker
            # then sits forever because nothing re-drains it at boot.
            # Stretch the window to cover the bootstrap; the drain
            # returns as soon as claude is up (it does NOT block the
            # full window — see ``_drain_at_boot``).
            effective_timeout = boot_drain_timeout_s
            if list(getattr(config, "startup_commands", []) or []):
                effective_timeout = max(boot_drain_timeout_s, _STARTUP_BOOT_DRAIN_S)
            self._drain_at_boot(config, timeout_s=effective_timeout)
        if started and inject_startup_prompts:
            # Boot-mission injection (lead a2a 4973264a, 2026-06-14):
            # parity with SDK runtime — feed spec.startup_prompts to
            # claude as the first turn(s) on start. Without this a
            # flipped TUI agent sits at an empty ❯ — operator-facing
            # regression. Best-effort: failure logs + continues.
            self._inject_startup_prompts(config)
        if started:
            # A2A wake-on-push parity: give the interactive TUI the same
            # ``/v1/turn`` endpoint the SDK runner serves so a bus-pushed
            # message (the ``sac mcp channel`` subscriber's wake POST)
            # DRIVES a turn in the idle TUI instead of timing out on a dead
            # port. Best-effort — a failed bridge must not fail the start.
            self._maybe_start_turn_bridge(config)
        return started

    def _maybe_start_turn_bridge(self, config: AgentConfig) -> None:
        """Start the A2A turn bridge (best-effort; lazy default seam).

        Resolves the real launcher lazily to avoid the import cycle
        (:mod:`_tui_turn_bridge` imports this module). Tests inject a
        recording ``turn_bridge_start`` so no subprocess is spawned. A
        no-op for agents without a resolved ``a2a.port`` (the launcher
        itself returns None — see :func:`_tui_turn_bridge.start_turn_bridge`).
        """
        import logging

        fn = self._turn_bridge_start
        if fn is None:
            from ._tui_turn_bridge import start_turn_bridge

            fn = start_turn_bridge
        try:
            fn(config)
        except Exception as exc:  # stx-allow: fallback (reason: a bridge spawn failure must never wedge agent start — the agent still runs, only wake-on-push is degraded; logged for the operator)
            logging.getLogger(__name__).warning(
                "TuiSessionRuntime: A2A turn bridge failed to start for %s: %s",
                getattr(config, "name", "?"),
                exc,
            )

    def _maybe_stop_turn_bridge(self, config: AgentConfig) -> None:
        """Stop the A2A turn bridge (best-effort; lazy default seam)."""
        import logging

        fn = self._turn_bridge_stop
        if fn is None:
            from ._tui_turn_bridge import stop_turn_bridge

            fn = stop_turn_bridge
        try:
            fn(config)
        except Exception as exc:  # stx-allow: fallback (reason: bridge teardown is best-effort; a failure must not block stop())
            logging.getLogger(__name__).warning(
                "TuiSessionRuntime: A2A turn bridge failed to stop for %s: %s",
                getattr(config, "name", "?"),
                exc,
            )

    def _inject_startup_prompts(self, config: AgentConfig) -> None:
        """Feed spec.startup_prompts as the first user turn(s).

        Each prompt = separate turn via ``send_text_and_submit``, gated
        on ``wait_until_input_ready`` BEFORE the send and followed by a
        defensive trailing ``Enter`` keystroke. Empty list → no-op.
        Per-prompt failure logged and skipped; total failure does NOT
        raise so the supervisor restart cycle never oscillates.

        P0 fix (2026-06-15, operator-reported): figrecipe + todo +
        neurovista all booted but stalled because the prompt was pasted
        into the input field without an Enter actually submitting it.
        ``send_text_and_submit`` does issue Enter, but during the
        post-boot Ink-mount window claude can eat that Enter while
        still binding the input. The fix:

          1. Gate on ``wait_until_input_ready`` (the same gate
             ``send_turn`` uses) so the keystrokes never land on a
             not-yet-bound input.
          2. Append an explicit defensive ``Enter`` via
             ``send_keys(name, "Enter")`` — operator-recovered each
             stuck agent by attaching tmux and pressing this. Baking
             it in removes the manual rescue.
        """
        import logging

        log = logging.getLogger(__name__)
        prompts = list(getattr(config, "startup_prompts", []) or [])
        if not prompts:
            return
        name = session_name_for(config)
        for index, prompt in enumerate(prompts, start=1):
            if not prompt:
                continue
            try:
                self.wait_until_input_ready(config)
                self._mux.send_text_and_submit(name, prompt)
                self._mux.send_keys(name, "Enter")
                log.info(
                    "TuiSessionRuntime: injected startup_prompt %d/%d "
                    "(%d chars) into %s (with defensive Enter)",
                    index,
                    len(prompts),
                    len(prompt),
                    name,
                )
            except Exception as exc:  # stx-allow: fallback (per-prompt best-effort)
                log.warning(
                    "TuiSessionRuntime: startup_prompt %d/%d failed for %s: %s",
                    index,
                    len(prompts),
                    name,
                    exc,
                )

    def stop(self, config: AgentConfig) -> bool:
        """Kill the tmux session sac owns for this agent.

        Returns True iff a session existed AND has been terminated.
        A no-op-on-absent-session contract (matches the existing
        ``TmuxManager.stop`` semantics so the supervisor's
        ``stop()->start()`` cycle remains idempotent).
        """
        # Tear down the A2A turn bridge first so it stops accepting wake
        # POSTs before the tmux session it injects into goes away.
        self._maybe_stop_turn_bridge(config)
        name = session_name_for(config)
        return bool(self._mux.stop(name))

    def is_running(
        self, config: AgentConfig, max_idle_s: float = _DEFAULT_MAX_IDLE_S
    ) -> bool:
        """True iff sac's tmux session for this agent is **responsive**.

        Step 4/4 of the TUI hedge (lead a2a
        ``d383f5389dc548a49a293bffe390d619``): risk-2's deferred half.
        Before step 4, this method only proved the multiplexer session
        existed — a hung-but-alive inner ``claude`` process (e.g. an
        infinite-loop in the Ink renderer that never exits but never
        reads stdin either) would still return True and the supervisor
        would never restart it. Now the probe additionally requires
        pane activity within the last ``max_idle_s`` seconds, so the
        supervisor's existing ``RestartPolicy`` automatically catches
        the inner-hang case via the same is_running poll it already
        runs.

        Implementation: ``tmux display -p '#{session_activity}'``
        advances on every pane read OR write, so a responsive TUI
        keeps its activity stamp fresh (banner draws, periodic Ink
        re-renders, operator input, ``send_turn``). A hung claude
        stops writing; a deadlocked claude stops reading. Either way
        the stamp goes stale and ``is_running`` flips False.

        ``max_idle_s`` defaults to 300s — the same window the SDK
        runtime's ``health.interval`` default uses — so a quiet but
        healthy TUI gets a generous grace window before the
        supervisor restarts it. Callers (notably a custom
        ``spec.health`` policy) can tighten or loosen this per-poll.

        Returns ``False`` when the session is absent, when
        ``session_activity`` is unavailable (legacy multiplexer fakes
        that don't implement it return ``None``), or when the stamp
        is older than ``max_idle_s``.
        """
        name = session_name_for(config)
        if not self._mux.exists(name):
            return False
        activity = self._mux.session_activity(name)
        if activity is None:
            return False
        return (time.time() - float(activity)) <= max_idle_s

    def send_turn(
        self,
        config: AgentConfig,
        text: str,
        *,
        wait_ready: bool = True,
    ) -> bool:
        """Deliver one turn of input to the in-tmux TUI.

        Uses the multiplexer's ``send_text_and_submit`` (text first,
        settle delay, then a separate ``Enter`` keystroke) — the same
        primitive the salvaged ``claude_code._run_startup_commands``
        uses to defeat the "Enter dropped during a TUI re-render"
        race the operator reported in #353.

        Returns ``False`` (and skips the send) when sac's tmux session
        for this agent does not exist; this is the TUI analogue of
        the SDK runtime's "no live HTTP turn endpoint" guard — it
        lets a caller distinguish "delivered" from "no runtime to
        deliver to" without inspecting the multiplexer directly.

        Step 3 of the TUI hedge (lead a2a
        ``d383f5389dc548a49a293bffe390d619`` + clarification
        ``edfe809e55a24640b6a42318872c8b58``): this is the
        delivery-side primitive; the response-side primitive is
        ``logs(...)`` via ``mux.capture_logs``. End-to-end turn
        completion = ``send_turn`` then poll ``logs`` for the
        expected response token. Step 3 is intentionally hermetic
        (auth- and network-independent): the real-binary suite
        exercises delivery via a deterministic stand-in command
        (``bash -c 'cat'``) so the test proves DELIVERY through
        real tmux without coupling to operator credentials or
        Anthropic's API. Authenticated-claude "answers a turn"
        verification is owned by the step-4 tui-alive integration
        probe, gated on credentials being present.
        """
        name = session_name_for(config)
        if not self._mux.exists(name):
            return False
        if wait_ready:
            # State-table dispatch (lead a2a
            # ``286ce8f625744cd08e4ee23eddf2c7aa``, 2026-06-14):
            # before delivering the turn, drain any first-launch /
            # mid-session modals via the existing
            # ``runtimes/prompts.py`` registry (theme picker, login
            # method, file-trust, dev-channels, etc. — 12 handlers
            # today, all keystroke-driven). The driver no longer
            # ad-hoc-detects gates inline; the SAME registry that
            # the SDK runtime uses serves the TUI runtime.
            #
            # Skippable via ``wait_ready=False`` for the in-memory
            # unit suite where the multiplexer fake doesn't render
            # an input-ready marker (the modal drain has no work
            # to do anyway). Real-binary callers always wait.
            self.wait_until_input_ready(config)
        # Bare send (lead a2a c6707941, 2026-06-14): operator's
        # diagnostic on the live host proved `tmux send-keys <text>`
        # + `tmux send-keys Enter` reaches the claude TUI input
        # field and submits cleanly every time. The verified
        # variant's echo-detection (substring match on Ink-rendered
        # capture-pane glyphs) was raising TuiKeystrokeDropError
        # despite the keystrokes landing — net effect was a 60s
        # `sac agents send` timeout despite a working pipeline.
        # Switch the default back to the bare primitive (the same
        # one claude_code._run_startup_commands uses against
        # startup_prompts).
        self._mux.send_text_and_submit(name, text)
        return True

    def _drain_at_boot(
        self,
        config: AgentConfig,
        *,
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> bool:
        """Dismiss claude's first-run modals at boot; return as soon as
        the TUI is up — NOT when it goes idle.

        Differs from :meth:`wait_until_input_ready` (the send_turn drain,
        which waits for the ``? for shortcuts`` idle marker) on two
        boot-specific realities:

          * ``startup_commands`` can delay ``exec claude`` by minutes —
            the loop just keeps polling (nothing in the install log
            matches a modal) until claude finally renders its first
            screen. The caller passes a window wide enough to cover the
            bootstrap (:data:`_STARTUP_BOOT_DRAIN_S`).
          * an autonomous agent with ``startup_prompts`` goes STRAIGHT to
            work once the modals clear, so the idle marker may never
            show. Exit on the marker OR :func:`prompts.is_ready` (claude
            launched with no blocking ``Enter to confirm`` modal),
            whichever first — so start() never blocks the full window
            after the bypass/trust picker is dismissed.

        Best-effort: never raises. A timeout just means the modal will be
        re-drained by the next :meth:`send_turn`. Returns True iff a
        ready signal was observed within the window.
        """
        name = session_name_for(config)
        if not self._mux.exists(name):
            return False
        accepted: set[str] = set()
        marker = "? for shortcuts"
        deadline = time.monotonic() + timeout_s
        send_keys_fn = self._mux.send_keys
        while time.monotonic() < deadline:
            pane = self._mux.capture_content(name)
            if marker in pane or _prompts.is_ready(pane):
                return True
            handled = _prompts.detect_and_respond(
                pane, accepted, send_keys_fn=lambda key: send_keys_fn(name, key)
            )
            if handled is not None:
                accepted.add(handled)
                continue
            if poll_s > 0:
                time.sleep(poll_s)
        import logging

        logging.getLogger(__name__).warning(
            "TuiSessionRuntime: boot-drain window (%.0fs) elapsed for %s "
            "without a ready signal (drained %d modal(s): %s); send_turn "
            "will re-drain.",
            timeout_s,
            name,
            len(accepted),
            sorted(accepted),
        )
        return False

    def wait_until_input_ready(
        self,
        config: AgentConfig,
        *,
        timeout_s: float = 60.0,
        poll_s: float = 0.4,
        sleep_fn=time.sleep,
    ) -> bool:
        """Drain first-launch / mid-session modals, then block until the
        TUI input field is bound.

        Lead a2a ``286ce8f625744cd08e4ee23eddf2c7aa`` (2026-06-14): the
        TUI driving is no longer a tower of ad-hoc inline detections.
        Each polling frame is matched against the existing
        :mod:`runtimes.prompts` registry (12 handlers today —
        theme-selection / login-method / file-trust / dev-channels /
        bypass-permissions / press-enter-continue / external-imports /
        mcp-json-edit / thinking-effort / skip-permissions-yn /
        compose-pending-unsent / file-trust-radio); the FIRST matching
        handler runs its registered keystrokes (``send_keys`` via the
        multiplexer) and is added to the ``accepted`` set so a
        re-render of the same modal doesn't double-send. Polling
        continues until either the ``? for shortcuts`` input-ready
        marker appears OR :class:`TuiInputNotReadyError` is raised on
        timeout.

        The 401 / rate-limited / processing / response-ready states
        the operator named in the new spec land in the SAME registry
        as additional handlers (see :mod:`runtimes.prompts`); when
        their detect strings match, the registered action runs
        without further driver changes.

        ``poll_s`` defaults to 400ms — long enough that the
        capture-pane subprocess doesn't dominate the wall, short
        enough that Ink modal re-renders are caught within a few
        frames.
        """
        name = session_name_for(config)
        if not self._mux.exists(name):
            raise TuiInputNotReadyError(
                f"TUI session {name!r} does not exist; nothing to wait for."
            )

        # ``accepted`` is intentionally per-call: a fresh modal
        # sequence on each turn (e.g. a context-window press-enter)
        # should be dismissed afresh.
        accepted: set[str] = set()
        marker = "? for shortcuts"
        deadline = time.monotonic() + timeout_s
        last_pane = ""
        send_keys_fn = self._mux.send_keys
        while time.monotonic() < deadline:
            last_pane = self._mux.capture_content(name)
            if marker in last_pane:
                return True
            handled = _prompts.detect_and_respond(
                last_pane, accepted, send_keys_fn=lambda key: send_keys_fn(name, key)
            )
            if handled is not None:
                accepted.add(handled)
                # Re-capture immediately after a keystroke — the
                # modal may dismiss in <poll_s and the input field
                # bind on the very next frame.
                continue
            if poll_s > 0:
                sleep_fn(poll_s)
        raise TuiInputNotReadyError(
            f"TUI input-ready marker {marker!r} not seen in pane "
            f"{name!r} within {timeout_s:.1f}s after draining "
            f"{len(accepted)} modal(s) ({sorted(accepted)}). "
            f"Last pane content:\n{last_pane}"
        )

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Return the last ``lines`` of pane output for the session.

        Empty string when the session does not exist (the supervisor
        can distinguish "no logs because no session" from "session
        alive but quiet" by checking ``is_running`` first).
        """
        name = session_name_for(config)
        if not self._mux.exists(name):
            return ""
        return str(self._mux.capture_logs(name, lines=lines))
