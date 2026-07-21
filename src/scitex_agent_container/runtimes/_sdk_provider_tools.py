"""Provider-aware tool REGISTRATION whitelist (PR #319).

Extracted verbatim from :mod:`._sdk_common` (line-cap split, 2026-07-21 —
see ``GITIGNORED/REFACTORING.md`` protocol): the constant, its root-cause
record, and the ``build_sdk_options`` apply block now live here as one
focused module. No behavior change.

PR #319 (lead msg a456b610 2026-06-06): provider-aware tool whitelist.

Root cause v8: LiteLLM 1.52.16's Anthropic-shim doesn't recognize newer
Claude Code builtins (ExitPlanMode, BashOutput, KillShell — added after
the LiteLLM version pinned in the cohort's vLLM stack). The shim's
pydantic Union of recognized AnthropicTool subclasses falls through to
the last subclass (``AnthropicComputerTool``), which requires
``display_width_px`` that the unknown tool's payload doesn't have →
422 on every API call → capsule errors all 60 turns.

Fix: when a provider backend is active, REGISTER only the
shim-recognized tool set so unrecognized builtins never enter the
outbound ``tools[]`` array. ``ClaudeAgentOptions.tools`` is the
registration knob — maps to ``--tools <csv>`` per the SDK transport
layer at ``claude_agent_sdk/_internal/transport/subprocess_cli.py
:241-250``, where the CLI honours it as "the list of available tools
from the built-in set" (CLI ``--help``).

Spec-side override: ``spec.claude.provider.allowed_tools: list[str]``
lets an operator declare their shim's recognized set explicitly. When
absent, the default below applies. The default is calibrated for
LiteLLM-1.52.16-known tools (clew bm172 cohort 2026-06-06 baseline) +
the ``Agent`` subagent registrar; bump this list as the shim ecosystem
catches up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..config._types import AgentConfig

_PROVIDER_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Task",
    "NotebookEdit",
    "Agent",
)


def apply_provider_tools(kwargs: dict[str, Any], config: "AgentConfig | None") -> None:
    """Populate ``kwargs['tools']`` for provider-backed agents (in place).

    Resolution order (see module docstring for the root-cause contract):

      1. ``spec.claude.provider.allowed_tools`` (operator override) —
         used verbatim; the operator KNOWS their shim's recognized set.
      2. :data:`_PROVIDER_DEFAULT_ALLOWED_TOOLS` (runner default) — the
         LiteLLM-1.52.16-known set + Agent.

    Non-provider agents (real Anthropic backend, or ``config is None``)
    leave ``tools`` unset → CLI registers its full default toolset
    (back-compat). An explicit caller-provided ``tools`` in ``kwargs``
    WINS over this auto-populate.
    """
    from ._apptainer_provider import provider_active

    if config is None or not provider_active(config) or "tools" in kwargs:
        return
    provider = getattr(getattr(config, "claude", None), "provider", None)
    spec_tools = list(getattr(provider, "allowed_tools", []) or [])
    if spec_tools:
        kwargs["tools"] = spec_tools
    else:
        kwargs["tools"] = list(_PROVIDER_DEFAULT_ALLOWED_TOOLS)


__all__ = [
    "_PROVIDER_DEFAULT_ALLOWED_TOOLS",
    "apply_provider_tools",
]
