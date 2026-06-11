"""Runtime selection + fallback-workdir helpers for the lifecycle layer.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names so existing
``lc._get_runtime`` / ``lc._fallback_workdir`` call sites are unchanged.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig


def _get_runtime(config: AgentConfig):
    """Return the agent runtime (SDK or tmux) for the config.

    Day-2 (E) — branches on ``spec.claude.runtime``:

    * ``"sdk"`` (default) → ``ClaudeSessionRuntime`` running
      ``claude-agent-sdk`` over the post-2026-06-15 Agent SDK credit.
    * ``"tmux"`` → ``ClaudeCodeRuntime`` driving the interactive
      ``claude`` TUI through tmux send-keys / capture-pane, preserving
      flat-rate subscription economics for the SAC fleet.

    Container-engine selection (``spec.runtime``) is unchanged —
    apptainer-only since the 2026-05-13 ripout; empty / unset is
    treated as ``"apptainer"``.
    """
    claude_runtime = getattr(getattr(config, "claude", None), "runtime", "sdk")
    if claude_runtime == "tmux":
        from .._runners._tmux.claude_code import ClaudeCodeRuntime

        return ClaudeCodeRuntime()

    if config.runtime in ("", "apptainer"):
        from ..runtimes.claude_session import ClaudeSessionRuntime

        return ClaudeSessionRuntime()
    raise ValueError(
        f"Unsupported runtime: {config.runtime!r}. "
        "Sac is apptainer-only since 2026-05-13."
    )


def _fallback_workdir(name: str) -> str:
    """Return the workdir path used when the agent's YAML can't be loaded.

    Lands under sac's own user-state dir
    (``~/.scitex/agent-container/runtime/workspaces/<name>/``) per the
    local-state-directories spec — sac never writes to another package's tree.
    """
    return str(
        Path.home() / ".scitex" / "agent-container" / "runtime" / "agents" / name
    )
