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

Key-resolution via scitex-config
--------------------------------

The provider API key is resolved through the SciTeX-ecosystem standard
``direct → config → env → default`` cascade exposed by
``scitex_config``:

  1. ``scitex_config.load_dotenv(dotenv_path=$HOME/.env)`` merges the
     host-level ``$HOME/.env`` into ``os.environ`` **without overriding
     any already-set var** — an explicit ``export DEEPSEEK_API_KEY=...``
     in the launch shell still wins. Path is pinned to ``$HOME/.env``
     on purpose: the default ``load_dotenv`` order also checks
     ``cwd/.env`` first, which would be a surprise for an operator
     running ``sac agents start`` from an unrelated project dir.
  2. ``scitex_config.PriorityConfig(auto_uppercase=False).resolve(
     key=auth_token_env, default="")`` then reads the value from
     ``os.environ`` (the only layer populated for this resolver —
     no YAML config_dict by design; secrets do not belong in YAML).
     ``auto_uppercase=False`` because ``auth_token_env`` is already
     the literal env-var name declared in ``spec.claude.provider``.
     ``PriorityConfig`` auto-masks ``API_KEY`` / ``TOKEN`` / ``SECRET``
     style keys in its resolution log, so the value is never logged.

Where the key lives, in operator-facing terms:

  * ``export DEEPSEEK_API_KEY=sk-...`` in the launch shell, OR
  * ``DEEPSEEK_API_KEY=sk-...`` as a line in ``$HOME/.env`` (chmod 0600).

The in-container ``$HOME/.env`` materialized by ``runtimes/_to_home.py``
from each agent's ``to_home/.env`` is a SEPARATE flow (it equips the
agent INSIDE the container with a ``.env``) and is not consulted for
host-side provider env resolution.

Fail-loud (never silent fallback):

* ``auth_token_env`` resolves to empty after the scitex-config cascade
  → :class:`ProviderEnvError`. A silent fallback would route the agent
  to Anthropic on no key (every turn 401s) with a fresh-looking
  heartbeat.
* ``spec.claude.provider`` AND ``spec.claude.account`` both set →
  :class:`ProviderEnvError`. The two auth paths are mutually exclusive;
  the validator already rejects this at load time, but the runtime
  guards again so a hand-built ``AgentConfig`` can't sneak past.
"""

from __future__ import annotations

from pathlib import Path

from scitex_config import PriorityConfig, load_dotenv

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
    * ``provider.auth_token_env`` resolves to empty after the
      scitex-config cascade (see module docstring).

    The API key VALUE is read here and embedded in the argv but never
    logged by sac (``PriorityConfig`` masks it in its resolution log).
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

    # SciTeX-ecosystem precedence: shell-export > $HOME/.env > default.
    # load_dotenv() is no-op-safe — already-set process env always wins,
    # so calling it on every provider-env resolution is cheap and
    # idempotent. Path is pinned to $HOME/.env to avoid the cwd-first
    # surprise of the default load_dotenv() search order.
    load_dotenv(dotenv_path=str(Path.home() / ".env"))
    resolver = PriorityConfig(auto_uppercase=False)
    api_key = resolver.resolve(key=auth_token_env, default="")
    if not api_key:
        raise ProviderEnvError(
            f"spec.claude.provider.auth_token_env='{auth_token_env}' could "
            "not be resolved through scitex-config (direct → config → env "
            "→ default cascade). Set the key by EITHER exporting "
            f"{auth_token_env} in the shell that runs `sac agents start` "
            f"OR adding the line `{auth_token_env}=...` to $HOME/.env "
            "(chmod 0600). sac reads the value at start and never logs "
            "it; PriorityConfig auto-masks it in the resolution log."
        )

    # Per-agent clean config dir — the conflict-breaker. Distinct from the
    # OAuth path's /tmp/sac-claude so a stale OAuth bind can never win.
    config_dir = f"/tmp/sac-{config.name}-provider-cfg"
    flags = [
        "--env",
        f"ANTHROPIC_BASE_URL={base_url}",
        "--env",
        f"SAC_ANTHROPIC_API_KEY={api_key}",
        # Also set ANTHROPIC_API_KEY directly. The SDK runtime bridges
        # SAC_ANTHROPIC_API_KEY -> ANTHROPIC_API_KEY inside its python
        # runner, but the TUI runtime's inner process is `claude` (no such
        # bridge), so without this the TUI gets ANTHROPIC_BASE_URL but no
        # API key and falls back to Claude.com OAuth instead of the
        # provider backend (cohort-A Qwen/LiteLLM de-risk 2026-06-23: the
        # TUI sat at the OAuth sign-in screen until poll_guard timed out).
        # In provider mode the OAuth creds bind is skipped, so a direct
        # ANTHROPIC_API_KEY cannot collide with it.
        "--env",
        f"ANTHROPIC_API_KEY={api_key}",
        "--env",
        f"CLAUDE_CONFIG_DIR={config_dir}",
    ]
    # ADR-0011 extension (lead-learnings/05 pitfall fix): auto-inject
    # ``ANTHROPIC_MODEL`` from ``spec.claude.model`` whenever a provider
    # is active. Without this, the SDK's built-in default model id wins
    # over the spec — every turn talks to the provider's gateway but
    # against the wrong model alias, and operators have to duplicate
    # the model id into ``raw_args.env`` to work around it. Skipped
    # when ``model`` is empty (the SDK's default is then intentional).
    model = (getattr(claude, "model", "") or "") if claude is not None else ""
    if model:
        flags.extend(["--env", f"ANTHROPIC_MODEL={model}"])
    return flags


__all__ = ["ProviderEnvError", "provider_active", "provider_env_flags"]
