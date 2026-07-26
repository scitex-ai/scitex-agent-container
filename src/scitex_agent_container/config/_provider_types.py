"""ProviderSpec dataclass for ``spec.claude.provider`` (backend override).

Lives in its own module to keep ``_types.py`` under the project's
512-line cap (mirrors ``_proxy_types.py``). Re-exported from
:mod:`scitex_agent_container.config` alongside the rest of the spec
dataclasses.

Also home to :data:`AgentProvider` (``spec.provider``, TOP-LEVEL) — see
the naming-collision note below before touching either symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Agent SDK family selector — spec.provider (TOP-LEVEL; openai-compat-1
# foundation, scitex-todo card)
# ---------------------------------------------------------------------------
#
# NAMING COLLISION WARNING: this module ALSO defines :class:`ProviderSpec`
# below, for the pre-existing ``spec.claude.provider`` (nested) — a vendor-
# agnostic ANTHROPIC-COMPATIBLE backend override (DeepSeek, Mimo/Xiaomi, a
# self-hosted gateway, ...). That field still runs the SAME
# ``claude-agent-sdk``, just pointed at a different ``base_url`` via an API
# key instead of Anthropic OAuth (see ``_provider_registry.PROVIDERS`` /
# ``runtimes/_apptainer_provider.py``).
#
# :data:`AgentProvider` is a DIFFERENT axis entirely: ``spec.provider``
# (top-level, sibling of ``spec.runtime`` — see ``config._types.AgentConfig``)
# selects WHICH AGENT SDK / tool-calling framework backs the session at all —
# ``anthropic`` (``claude-agent-sdk``, talking to Anthropic OR an Anthropic-
# compatible gateway) vs ``openai`` (the ``openai-agents`` SDK, talking to
# OpenAI's Agents/Responses API). The two axes COMPOSE, they don't collide in
# practice — e.g. ``provider: anthropic`` (top-level, default) +
# ``claude.provider: mimo`` (nested) runs the Claude Agent SDK against
# Xiaomi's Anthropic-compatible endpoint. Do not conflate the two fields when
# reading/writing either one.
#
# Landed foundation-only: the schema + dataclass field exist and validate,
# but nothing branches on ``AgentConfig.provider`` yet — selecting
# ``"openai"`` today only affects `sac agents explain` / config introspection.
# openai-compat-2 lands the concrete ``openai`` runner (see
# ``_runners/_provider_session.py`` for the ``ProviderSession`` Protocol it
# will implement); openai-compat-3 wires the selection into the apptainer
# entrypoint + container image.
AgentProvider = Literal["anthropic", "openai"]

DEFAULT_AGENT_PROVIDER: AgentProvider = "anthropic"


@dataclass
class ProviderSpec:
    """Vendor-agnostic backend override for the Claude SDK session.

    When set under ``spec.claude.provider``, the agent's SDK session
    talks to an Anthropic-SDK-compatible backend OTHER than Anthropic
    (DeepSeek, a self-hosted gateway, etc.) using a never-expiring,
    login-free API key instead of Anthropic OAuth. Lets bulk fleet
    work run on a cheap backend without burning Max-plan quota.

    The runtime injects three env vars into the container at start
    (see ``runtimes/_apptainer_provider.py``):

    * ``ANTHROPIC_BASE_URL`` ← :attr:`base_url`
    * ``SAC_ANTHROPIC_API_KEY`` ← the host value of ``$<auth_token_env>``
      (bridged to ``ANTHROPIC_API_KEY`` for the SDK by sac's existing
      auth handoff). Fails loud at start if the env var is unset.
    * ``CLAUDE_CONFIG_DIR`` ← a clean per-agent dir — the conflict-breaker
      so the OAuth ``.credentials.json`` bind cannot win (apptainer
      ``--env`` is last-wins).

    The model id is the provider's own (e.g. ``deepseek-chat``); the
    claude-* regex in :mod:`config._validation` is relaxed whenever a
    provider is declared.

    Mutually exclusive with :attr:`ClaudeSpec.account` (OAuth pin) — an
    API-key backend needs no OAuth, so declaring both is a config error
    the runtime rejects loudly.
    """

    name: str = ""
    """Registered backend name when the mapping form uses
    ``provider: {name: codex, ...}``. Empty for legacy explicit URL mappings."""

    base_url: str = ""
    """Anthropic-compatible base URL, e.g.
    ``https://api.deepseek.com/anthropic``. Required when the provider
    block is present."""

    auth_token_env: str = ""
    """NAME of the host env var holding the API key (e.g.
    ``DEEPSEEK_API_KEY``) — NOT the key value. The operator sources the
    secret file; sac reads the env var's value at start and never logs
    it. Required when the provider block is present."""

    auth_token: str = field(default="", repr=False)
    """Declarative authentication value.

    ``auto`` means resolve/generate the value at launch. Any other non-empty
    value is used directly; this supports private, untracked specs, while
    tracked specs should use ``auto`` so secrets never enter Git.
    """

    allowed_tools: list[str] = field(default_factory=list)
    """Whitelist of built-in Claude Code tools to REGISTER when this
    provider is active. Maps directly to
    ``ClaudeAgentOptions.tools`` (the SDK ``--tools <csv>`` knob, see
    ``claude_agent_sdk/_internal/transport/subprocess_cli.py:241-250``),
    which controls the REGISTERED tool set rather than the use-allow
    list (``--allowedTools`` / ``--disallowedTools`` only affect
    permission, not whether the tool's JSON-schema ships in the
    outbound API request body — confirmed empirically by clew bm172
    cohort v7 bind-test 2026-06-06).

    Why this matters: an Anthropic-API shim (LiteLLM / vLLM /
    gateway) may not recognize newer Claude Code built-ins
    (``ExitPlanMode``, ``BashOutput``, ``KillShell`` were added
    after LiteLLM 1.52.16). When the shim sees such a tool in the
    outbound ``tools[]`` it falls through its pydantic Union to the
    last subclass (``AnthropicComputerTool``), which requires
    ``display_width_px`` that the unknown tool's payload doesn't
    have → 422 on every API call → capsule errors all 60 turns.
    Whitelisting only the shim-recognized tools suppresses the
    unrecognized builtins at REGISTRATION, so they never enter
    the outbound body.

    Empty list (the default) means "use the runner's old-stable
    default whitelist when the provider is active, otherwise leave
    ``tools`` unset and let the CLI's full default register" — the
    runner picks the safe shape automatically.

    The string-form provider (e.g. ``provider: mimo``) doesn't carry
    this field today; the per-registry ``allowed_tools`` plumbing is a
    follow-up. Operators on the string form fall back to the runner's
    old-stable default until the registry plumbing lands.
    """
