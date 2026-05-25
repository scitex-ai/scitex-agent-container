"""Vendor-agnostic backend-override env injection for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_creds``, ``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``).

When ``spec.claude.provider`` is set, the agent's SDK session runs
against an Anthropic-SDK-compatible backend (DeepSeek, a self-hosted
gateway, ...) on a never-expiring API key instead of Anthropic OAuth.
The proven recipe (validated end-to-end with A/B network proof) is to
inject three env vars into the container at start:

* ``ANTHROPIC_BASE_URL``   ← ``provider.base_url``
* ``SAC_ANTHROPIC_API_KEY`` ← the HOST value of ``$<provider.auth_token_env>``
  (sac's existing handoff bridges it to ``ANTHROPIC_API_KEY`` for the SDK).
* ``CLAUDE_CONFIG_DIR``    ← a clean per-agent dir. CRITICAL conflict-breaker:
  apptainer ``--env`` is last-wins, so without forcing a fresh config dir
  the OAuth ``.credentials.json`` bind (mounted at ``/tmp/sac-claude``)
  would win and the SDK would talk to Anthropic, not the provider.

The caller (``_apptainer_runtime.build_run_argv``) must ALSO skip the
OAuth ``.credentials.json`` bind entirely when a provider is active —
an API-key backend needs no OAuth. :func:`provider_active` is the
single predicate that gates both the env injection and the bind skip.

Fail-loud (never silent fallback):

* ``auth_token_env`` names an env var that is unset/empty on the host →
  :class:`ProviderEnvError`. A silent fallback would route the agent to
  Anthropic on no key (every turn 401s) with a fresh-looking heartbeat.
* ``spec.claude.provider`` AND ``spec.claude.account`` both set →
  :class:`ProviderEnvError`. The two auth paths are mutually exclusive;
  the validator already rejects this at load time, but the runtime
  guards again so a hand-built ``AgentConfig`` can't sneak past.
"""

from __future__ import annotations

import os

from ..config import AgentConfig


class ProviderEnvError(RuntimeError):
    """Raised at start when a provider override cannot be satisfied."""


def _provider_spec(config: AgentConfig):
    """Return ``config.claude.provider`` or ``None`` (defensive getattr)."""
    claude = getattr(config, "claude", None)
    return getattr(claude, "provider", None) if claude is not None else None


def provider_active(config: AgentConfig) -> bool:
    """True when ``spec.claude.provider`` declares a usable backend override.

    A provider with an empty ``base_url`` is treated as inactive — the
    validator rejects that shape at load time, so this only guards
    hand-built configs and keeps the predicate honest.
    """
    provider = _provider_spec(config)
    return bool(provider is not None and getattr(provider, "base_url", ""))


def provider_env_flags(config: AgentConfig) -> list[str]:
    """Render the ``--env`` flags for an active provider override.

    Returns ``[]`` when no provider is active. Raises
    :class:`ProviderEnvError` (fail-loud) when:

    * the agent also pins ``spec.claude.account`` (mutually exclusive), or
    * ``provider.auth_token_env`` names an unset/empty host env var.

    The API key VALUE is read here and embedded in the argv but never
    logged by sac.
    """
    if not provider_active(config):
        return []

    provider = _provider_spec(config)
    claude = getattr(config, "claude", None)
    account = (getattr(claude, "account", "") or "") if claude is not None else ""
    if account:
        raise ProviderEnvError(
            "spec.claude.provider and spec.claude.account are mutually "
            "exclusive — a provider backend uses an API key, not Anthropic "
            f"OAuth (got account={account!r}). Set exactly one."
        )

    base_url = getattr(provider, "base_url", "")
    auth_token_env = getattr(provider, "auth_token_env", "")
    if not auth_token_env:
        raise ProviderEnvError(
            "spec.claude.provider.auth_token_env is empty; cannot resolve "
            "the backend API key. Set it to the NAME of the host env var "
            "holding the key (e.g. DEEPSEEK_API_KEY)."
        )

    api_key = os.environ.get(auth_token_env, "")
    if not api_key:
        raise ProviderEnvError(
            f"spec.claude.provider.auth_token_env='{auth_token_env}' names a "
            "host env var that is unset or empty. Source the secret file "
            f"that exports {auth_token_env} before starting this agent "
            "(sac reads its value at start and never logs it)."
        )

    # Per-agent clean config dir — the conflict-breaker. Distinct from the
    # OAuth path's /tmp/sac-claude so a stale OAuth bind can never win.
    config_dir = f"/tmp/sac-{config.name}-provider-cfg"
    return [
        "--env",
        f"ANTHROPIC_BASE_URL={base_url}",
        "--env",
        f"SAC_ANTHROPIC_API_KEY={api_key}",
        "--env",
        f"CLAUDE_CONFIG_DIR={config_dir}",
    ]


__all__ = ["ProviderEnvError", "provider_active", "provider_env_flags"]
