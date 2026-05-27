"""ProviderSpec dataclass for ``spec.claude.provider`` (backend override).

Lives in its own module to keep ``_types.py`` under the project's
512-line cap (mirrors ``_proxy_types.py``). Re-exported from
:mod:`scitex_agent_container.config` alongside the rest of the spec
dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass


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
