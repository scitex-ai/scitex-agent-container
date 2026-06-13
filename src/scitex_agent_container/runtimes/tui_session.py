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

from typing import Any

from .._runners._tmux.tmux import TmuxManager
from ..config import AgentConfig
from .base import RuntimeBase

__all__ = ["TuiSessionRuntime", "session_name_for"]


_CLAUDE_BIN_DEFAULT = "claude"


def session_name_for(config: AgentConfig) -> str:
    """Return the tmux session name owned by sac for this agent.

    ``tui-<agent-name>`` namespace-prefixes the session so it cannot
    collide with operator-owned tmux sessions on the same host;
    ``TmuxManager.exists`` keys off the same string so the runtime
    can probe its own session deterministically across processes.
    """
    return f"tui-{config.name}"


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

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        """Launch ``claude`` inside a detached tmux session sac owns.

        ``force=True`` stops any existing session for this agent
        before starting (the same idempotent-restart contract the
        supervisor relies on). ``dry_run=True`` skips the actual
        ``tmux new-session`` — the hedge doesn't materialise any
        on-disk workspace state yet, so dry-run is a no-op that
        returns True. ``foreground`` is ignored (a tmux session
        whose whole point is detachment has no meaningful foreground
        mode — same convention as the screen runner).
        """
        name = session_name_for(config)
        if force and self._mux.exists(name):
            self._mux.stop(name)
        if dry_run:
            return True
        workdir = getattr(config, "workdir", "") or "/tmp"
        return bool(
            self._mux.start(
                session_name=name,
                command=self._claude_bin,
                workdir=str(workdir),
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

    def is_running(self, config: AgentConfig) -> bool:
        """True iff sac's tmux session for this agent exists.

        This is risk-2's deferred half: a True return today only
        means the multiplexer session is alive, NOT that the inner
        ``claude`` process is responsive. The follow-up PR adds a
        tui-alive probe (pane-activity timestamp or sidecar
        heartbeat file) on top.
        """
        name = session_name_for(config)
        return bool(self._mux.exists(name))

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
