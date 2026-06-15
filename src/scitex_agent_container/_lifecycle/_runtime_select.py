"""Runtime selection + fallback-workdir helpers for the lifecycle layer.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names so existing
``lc._get_runtime`` / ``lc._fallback_workdir`` call sites are unchanged.

``spec.runtime`` semantics — operator directive 12870, lead a2a
``b58dd5d3b4d640d2a7f31f16c710e839``: the field was repurposed from
container-engine selector to LAUNCH-MODE selector. Accepted values
(validated in ``config/_validation.py:_VALID_RUNTIMES``):

  * ``claude-agent-sdk`` — headless SDK runner (the long-standing
    default; what every existing spec gets at dispatch).
  * ``tui``              — interactive tmux-backed Claude TUI session
    (the June-15 SDK-pool-cutoff pivot).
  * ``apptainer`` / ``""`` — back-compat: the pre-2026-06-13 container-
    engine values. Mapped to ``claude-agent-sdk`` here with a
    deprecation log so the existing spec corpus stays valid through
    the migration window without an op-day mass-rewrite.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AgentConfig

log = logging.getLogger(__name__)


def _get_runtime(config: AgentConfig):
    """Return the runtime adapter for the config's launch mode.

    Branches on ``config.runtime`` (the launch-mode selector).
    Default: an empty / unset ``runtime`` selects the interactive
    in-apptainer ``tui`` runtime (operator directive 2026-06-15).
    Back-compat: the legacy ``"apptainer"`` value (the old
    container-engine selector) maps to ``"claude-agent-sdk"``
    SILENTLY. The deprecation log for ``runtime='apptainer'`` fires
    at the actual start path
    (:func:`_lifecycle._start.start_agent`) — not here, because
    every status / list / discovery walk also goes through
    :func:`_get_runtime` and a per-call warning would (a) spam the
    operator's logs and (b) contaminate CLI output streams
    (CliRunner-captured commands like ``sac agents status --json``
    end up with the warning text in ``result.output``).
    Lead a2a ``f468a6d2e11443598103ed1672e2e40b``: emit the
    deprecation when the runtime is actually USED to start
    something, not on every read.
    """
    runtime = config.runtime or ""
    # TUI is the default launch mode (operator directive 2026-06-15,
    # post the SDK-pool cutoff): empty / unset → interactive in-apptainer
    # TUI. Explicit legacy values still map to the headless SDK runner so
    # existing SDK specs are untouched.
    if runtime in ("", "tui"):
        from ..runtimes.tui_session import TuiSessionRuntime

        return TuiSessionRuntime()
    if runtime in ("apptainer", "claude-agent-sdk"):
        from ..runtimes.claude_session import ClaudeSessionRuntime

        return ClaudeSessionRuntime()
    raise ValueError(
        f"Unsupported runtime: {runtime!r}. "
        "spec.runtime must be 'tui' (default, interactive in-apptainer "
        "TUI), 'claude-agent-sdk' (headless SDK runner), or the "
        "back-compat 'apptainer' (mapped to 'claude-agent-sdk')."
    )


def warn_if_legacy_apptainer_runtime(config: AgentConfig) -> None:
    """Emit the back-compat deprecation log if ``config.runtime`` is
    the legacy container-engine value.

    Called from :func:`_lifecycle._start.start_agent` (the actual
    start path) so the deprecation fires on a real launch, not on
    every status / list walk that also goes through
    :func:`_get_runtime`. NEVER raises — the deprecation is
    informational; failing to log it must not block a start.
    """
    runtime = getattr(config, "runtime", "") or ""
    if runtime != "apptainer":
        return
    log.warning(
        "spec.runtime='apptainer' is deprecated — the field was "
        "repurposed from container-engine to launch-mode on "
        "2026-06-13 (operator directive 12870). Treating as "
        "runtime='claude-agent-sdk' (the current default). "
        "Update %r's spec to runtime: claude-agent-sdk to silence "
        "this warning.",
        getattr(config, "name", "<unknown>"),
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
