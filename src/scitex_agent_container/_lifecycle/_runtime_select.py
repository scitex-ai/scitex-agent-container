"""Runtime selection + fallback-workdir helpers for the lifecycle layer.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names so existing
``lc._get_runtime`` / ``lc._fallback_workdir`` call sites are unchanged.

``spec.harness`` semantics — the harness axis (top-level,
``AgentHarness = Literal["anthropic", "openai"]`` — see
:mod:`config._harness_types`; ``spec.provider`` is the DEPRECATED
alias). A non-Anthropic harness is REFUSED here (v4 step-2 loudness,
card ``sac-v4-layering-refactor-harness-runtime-inference-20260813``):
the lifecycle launch path can only start the Claude harness until the
step-4 descriptor registry lands, and silently launching Claude under
an ``harness: openai`` spec is the wrong-vendor bug this guard retires.

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
from ..config._harness_types import ensure_harness_matches_claude_launch

log = logging.getLogger(__name__)


def _get_runtime(config: AgentConfig):
    """Return the runtime adapter for the config's launch mode.

    Branches on TWO axes:

    1. ``config.harness`` — the harness axis (``AgentHarness =
       Literal["anthropic", "openai"]``). This check runs FIRST so the
       harness always wins over ``runtime`` — and because every runtime
       this function can return launches the CLAUDE harness, a
       non-Anthropic harness is REFUSED loudly here instead of being
       dispatched (v4 step-2 loudness; harness-aware dispatch is the
       step-4 descriptor registry). The refusal replaces a DEAD read of
       ``getattr(config, "provider", None)`` — a field the harness
       rename removed from ``AgentConfig`` — which silently fell
       through and launched the Claude runner for ``harness: openai``
       specs.
    2. ``config.runtime`` — the launch-mode selector (operator
       directive 12870). Default: an empty / unset ``runtime``
       selects the interactive in-apptainer ``tui`` runtime
       (operator directive 2026-06-15). Back-compat: the legacy
       ``"apptainer"`` value (the old container-engine selector)
       maps to ``"claude-agent-sdk"`` SILENTLY. The deprecation
       log for ``runtime='apptainer'`` fires at the actual start
       path (:func:`_lifecycle._start.start_agent`) — not here,
       because every status / list / discovery walk also goes
       through :func:`_get_runtime` and a per-call warning would
       (a) spam the operator's logs and (b) contaminate CLI output
       streams (CliRunner-captured commands like
       ``sac agents status --json`` end up with the warning text
       in ``result.output``). Lead a2a
       ``f468a6d2e11443598103ed1672e2e40b``: emit the deprecation
       when the runtime is actually USED to start something, not
       on every read.
    """
    runtime = getattr(config, "runtime", "") or ""
    # v4 STEP-2 LOUDNESS (card sac-v4-layering-refactor-harness-runtime-
    # inference-20260813): every runtime below launches the CLAUDE
    # harness, so a non-Anthropic ``config.harness`` refuses here rather
    # than silently falling through (the fate of the old dead
    # ``getattr(config, "provider", None)`` read this replaces).
    # ``log=False``: _get_runtime also serves every status / list /
    # health walk — each degrades this raise into an UNKNOWN verdict
    # that keeps the message — and a per-read stderr line would
    # contaminate CliRunner-captured ``--json`` output (the same ruling
    # that placed the deprecation warnings below on the start path).
    ensure_harness_matches_claude_launch(
        config,
        launching=(
            "TuiSessionRuntime (the interactive Claude TUI)"
            if runtime in ("", "tui")
            else "ClaudeSessionRuntime (the headless claude-agent-sdk runner)"
        ),
        log=False,
    )
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
        "back-compat 'apptainer' (mapped to 'claude-agent-sdk'). "
        "There is no 'openai' runtime: OpenAI-harness agents cannot yet "
        "start through the lifecycle launch path (v4 gap, card "
        "sac-v4-layering-refactor-harness-runtime-inference-20260813) — "
        "drive them through a2a.handler: openai_session instead."
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


def warn_if_legacy_harness_key(config: AgentConfig) -> None:
    """Emit the deprecation log when the spec reached the harness axis
    through ``spec.provider`` instead of ``spec.harness``.

    Placed on the START path for the same reason as
    :func:`warn_if_legacy_apptainer_runtime` (lead a2a
    ``f468a6d2e11443598103ed1672e2e40b``): every status / list /
    discovery walk loads every definition on the host, so warning at
    LOAD time would print one line per spec on commands that asked no
    question about spec style, and would contaminate CliRunner-captured
    ``--json`` output. Warn when the key is actually USED to start
    something.

    NEVER raises — the deprecation is informational, and failing to log
    it must not block a start.
    """
    if not getattr(config, "harness_key_is_legacy", False):
        return
    log.warning(
        "spec.provider is DEPRECATED — the field selects the agent "
        "HARNESS (which agent SDK runs the session), not an inference "
        "provider, and is now spelled `harness:`. Still honoured, and "
        "resolving to harness=%r. Rename the key in %r's spec to "
        "`harness: %s` to silence this warning. (Unrelated to "
        "spec.claude.provider, which stays `provider` — it really does "
        "select an inference backend.)",
        getattr(config, "harness", ""),
        getattr(config, "name", "<unknown>"),
        getattr(config, "harness", ""),
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
