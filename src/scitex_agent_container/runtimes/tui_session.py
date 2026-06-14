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

import time
from pathlib import Path
from typing import Any

from .._runners._tmux.tmux import TmuxManager
from ..config import AgentConfig
from ._to_home import deploy_to_home
from ._tui_auth_stage import TuiAuthStageError, stage_tui_auth
from .base import RuntimeBase
from .claude_md import setup_claude_md

__all__ = [
    "TuiAuthStageError",
    "TuiSessionRuntime",
    "session_name_for",
    "stage_tui_auth",
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
    ) -> None:
        # Default to the real TmuxManager; tests pass an in-memory
        # MultiplexerProtocol fake. No mocks — the fake is a real
        # implementation of the same Protocol the real TmuxManager
        # satisfies, just backed by a dict instead of subprocess.
        self._mux = multiplexer if multiplexer is not None else TmuxManager
        self._claude_bin = claude_bin

    def materialize_workspace(self, config: AgentConfig) -> Path | None:
        """Materialise per-agent ``to_home/`` + CLAUDE.md + TUI-auth
        files into ``<state>/home/`` and return that path.

        Mirrors ``ClaudeSessionRuntime._materialize_workspace``: writes
        the sac-managed CLAUDE.md skill chain into ``<state>/home/CLAUDE.md``
        and overlays the per-agent ``to_home/`` (.mcp.json, .env,
        .claude/{hooks,skills,settings.json}) on top of the shared
        baseline. The result is a self-contained $HOME tree the TUI
        ``claude`` binary will read on launch — same SkillsSpec / MCP
        / hook surface the SDK runtime provides.

        ADDITIONAL TUI-auth staging (lead a2a
        ``910ff436642948eb85f8b3100204ed9b``, 2026-06-14): the
        interactive ``claude`` TUI checks TWO files the SDK runner
        does not — ``$HOME/.claude/.credentials.json`` (live OAuth
        token) and ``$HOME/.claude.json`` (onboarding state). Before
        this hook, every ``sac agents start --runtime tui`` agent sat
        on the login picker because neither file was present in the
        materialised HOME. :func:`stage_tui_auth` lands both, sourced
        by default from the apptainer auth bind + ``${HOME}/.claude.json``
        and overridable via ``SAC_TUI_AUTH_CREDENTIALS_SRC`` /
        ``SAC_TUI_AUTH_CLAUDE_JSON_SRC``. Fail-loud: a missing source
        raises :class:`TuiAuthStageError` with a remedy rather than
        letting the TUI silently stall on the picker.

        Returns ``None`` for stub configs lacking the full AgentConfig
        surface (unit-test ``SimpleNamespace`` fixtures); the caller
        treats that as "skip materialise, run with bare $HOME".
        """
        if not all(hasattr(config, a) for a in _REQUIRED_CONFIG_ATTRS):
            return None
        home_dir = state_dir_for_config(config) / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        setup_claude_md(config, str(home_dir))
        deploy_to_home(config, str(home_dir))
        stage_tui_auth(home_dir)
        return home_dir

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        """Launch ``claude`` inside a detached tmux session sac owns.

        Materialises the per-agent ``to_home/`` + CLAUDE.md into
        ``<state>/home/`` before launching tmux, then exports
        ``HOME=<state>/home`` + ``CLAUDE_CONFIG_DIR=<state>/home/.claude``
        into the tmux session so the in-tmux ``claude`` TUI sees the
        agent's skills, MCP servers, hooks, and settings.json — same
        surface the SDK runtime provides via apptainer bind-mount.

        ``force=True`` stops any existing session for this agent
        before starting (the same idempotent-restart contract the
        supervisor relies on). ``dry_run=True`` materialises the
        workspace but skips the actual ``tmux new-session`` — lets
        the operator verify the rendered tree without spawning a
        process. ``foreground`` is ignored (a tmux session whose
        whole point is detachment has no meaningful foreground mode
        — same convention as the screen runner).
        """
        name = session_name_for(config)
        if force and self._mux.exists(name):
            self._mux.stop(name)
        home_dir = self.materialize_workspace(config)
        if dry_run:
            return True
        workdir = getattr(config, "workdir", "") or "/tmp"
        env_exports = ""
        if home_dir is not None:
            env_exports = (
                f"export HOME={home_dir}\nexport CLAUDE_CONFIG_DIR={home_dir}/.claude\n"
            )
        return bool(
            self._mux.start(
                session_name=name,
                command=self._claude_bin,
                workdir=str(workdir),
                env_exports=env_exports,
            )
        )

    def stop(self, config: AgentConfig) -> bool:
        """Kill the tmux session sac owns for this agent.

        Returns True iff a session existed AND has been terminated.
        A no-op-on-absent-session contract (matches the existing
        ``TmuxManager.stop`` semantics so the supervisor's
        ``stop()->start()`` cycle remains idempotent).
        """
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

    def send_turn(self, config: AgentConfig, text: str) -> bool:
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
        self._mux.send_text_and_submit(name, text)
        return True

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
