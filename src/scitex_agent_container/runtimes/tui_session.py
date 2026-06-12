"""``tui`` runtime adapter — June-15 SDK-pool-cutoff TUI pivot (skeleton).

Operator directive 12861 (lead a2a ``e39ab77a181340b6975b6ad7aea70329``):
from June 15 the SDK / ``claude -p`` path becomes increasingly
disadvantageous for subscription users (the Max-pool cutoff —
``project_sdk_subscription_cutoff_tui_pivot``). The operator wants
the fleet to gradually shift weight to TUI mode while keeping sac
as the always-launcher (single SSoT = spec.yaml).

`spec.runtime: tui` selects this adapter. The bundled ``claude`` CLI
runs interactively inside a tmux session that sac owns; sac wires
the agent's MCP, healthchecks the TUI's responsiveness, and restarts
the inner process on crash — same lifecycle contract as
``ClaudeSessionRuntime`` for ``runtime: claude-agent-sdk``.

THIS MODULE IS A SKELETON. The wiring (start/stop/is_running/logs)
raises ``NotImplementedError`` with a structured pointer to the
follow-up PR. The four TUI-stability risks I named in the design
A2A (lead a2a ``c8edc13babb44852a8f547b1398e200b``) are baked into
the docstring so the implementing PR cannot ship without addressing
them:

  1. **Session-detach survival** — bare ``claude`` exits when the
     terminal closes. The TUI runner MUST spawn inside a multiplexer
     (tmux or screen — same convention ``_runners/_session_runner``
     and ``_runners/_screen_runner`` use). Operator interaction
     goes through ``tmux attach``; the agent process survives
     detaches and terminal-emulator restarts.
  2. **Health-check signal** — the existing ``sdk-alive`` probe
     queries an SDK-side response. TUI has no clean equivalent.
     Need a ``tui-alive`` method that watches tmux pane activity
     timestamp (or a heartbeat the TUI emits to a sidecar file on
     each prompt cycle). Without this, sac can't tell a frozen TUI
     from a healthy idle one — autorestart loops without observable
     rationale.
  3. **Restart-on-crash semantics** — Claude TUI segfaults / hangs
     happen. Existing restart policy works for processes; the TUI
     inside tmux needs sac to detect the INNER death (not just the
     tmux session existence) and restart the inner process. New
     responsibility for the tmux/screen runners.
  4. **Memory pressure under long-running TUI** — a multi-day TUI
     session accumulates scrollback + REPL state; OOM is a real
     concern that the SDK runner avoids by terminating after each
     turn. Mitigation: ``spec.context_management: auto-compact``
     parity inside the TUI runner so the operator can configure a
     rolling-summary cadence.

Cross-thread context: the hub Workspace-Console SIF rebuild (task
#11, hub card ``sif-claude-sdk-bundle``) ships the SDK-bundled
``claude`` CLI as ``/usr/local/bin/claude`` in the apptainer image
— the SAME binary this runner exec's. Lands in parallel; the TUI
runner picks up whatever ``claude`` version the rebuilt SIF provides.
"""

from __future__ import annotations

from ..config import AgentConfig
from .base import RuntimeBase

__all__ = ["TuiSessionRuntime"]


_NOT_IMPLEMENTED = (
    "TuiSessionRuntime is a skeleton landed alongside the "
    "spec.runtime field plumbing. Follow-up PR wires the tmux "
    "session + tui-alive health hook + inner-process restart "
    "adapter + auto-compact (the four TUI-stability risks named in "
    "the module docstring). Until then, runtime: tui specs are "
    "rejected at dispatch with this error rather than silently "
    "falling back — operator directive 12847 (fail-loud, no silent "
    "fallback)."
)


class TuiSessionRuntime(RuntimeBase):
    """Interactive tmux-backed Claude TUI runtime (skeleton).

    Selected by ``spec.runtime: tui``. The actual launch surface is
    wired in the follow-up PR; this class currently raises
    ``NotImplementedError`` at every entry point so a misconfigured
    ``runtime: tui`` spec fails LOUDLY at dispatch instead of
    silently doing nothing.
    """

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stop(self, config: AgentConfig) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def is_running(self, config: AgentConfig) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        raise NotImplementedError(_NOT_IMPLEMENTED)
