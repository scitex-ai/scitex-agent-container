"""ProviderSpec dataclass for ``spec.claude.provider`` (backend override).

Lives in its own module to keep ``_types.py`` under the project's
512-line cap (mirrors ``_proxy_types.py``). Re-exported from
:mod:`scitex_agent_container.config` alongside the rest of the spec
dataclasses.

ONE AXIS ONLY: the INFERENCE backend. This module used to also hold the
top-level ``spec.provider`` agent-SDK-family selector, under a standing
"NAMING COLLISION WARNING" telling readers not to conflate the two. That
axis is a HARNESS (which agent PROGRAM runs the loop), was renamed to
``spec.harness``, and moved to :mod:`config._harness_types` — so the
collision is retired rather than documented. ``spec.claude.provider``
here is the genuine provider axis: the SAME ``claude-agent-sdk``,
pointed at a different ``base_url`` with an API key instead of Anthropic
OAuth. The two axes COMPOSE — ``harness: anthropic`` (default) +
``claude.provider: mimo`` runs the Claude Agent SDK against Xiaomi's
Anthropic-compatible endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    base_url: str = ""
    """Anthropic-compatible base URL, e.g.
    ``https://api.deepseek.com/anthropic``. Required when the provider
    block is present."""

    auth_token_env: str = ""
    """NAME of the host env var holding the API key (e.g.
    ``DEEPSEEK_API_KEY``) — NOT the key value. The operator sources the
    secret file; sac reads the env var's value at start and never logs
    it. Required when the provider block is present."""

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
