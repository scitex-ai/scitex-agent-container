"""Runtime selection + fallback-workdir helpers for the lifecycle layer.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names so existing
``lc._get_runtime`` / ``lc._fallback_workdir`` call sites are unchanged.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig


def _get_runtime(config: AgentConfig):
    """Return the SDK runtime for the config.

    Sac is apptainer-only since the 2026-05-13 ripout. Empty / unset
    ``spec.runtime`` is treated as ``"apptainer"``.
    """
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
