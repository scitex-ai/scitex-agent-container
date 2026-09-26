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

OpenAI harness columns (openai-compat-3)
--------------------------------------------

This module ALSO owns the apptainer env story for the OTHER axis: the
TOP-LEVEL ``spec.harness`` selector (``anthropic`` | ``openai`` — see
:mod:`config._harness_types`; a DIFFERENT field from the nested
``spec.claude.provider`` the functions above serve, and still reachable
through its deprecated ``spec.provider`` alias).

:func:`resolve_agent_harness` resolves the harness for one launch —
``SAC_PROVIDER`` (host env) is the documented OPS-ONLY override, see its
docstring. :func:`openai_env_flags` renders the ``--env`` flags for an
``openai``-harness agent, mirroring the Anthropic-creds plumbing in
``_apptainer_auth.auth_argv``:

* ``SAC_OPENAI_API_KEY`` ← the host key, resolved through the same
  scitex-config cascade as above (shell export > ``$HOME/.env``), tried
  as ``SAC_OPENAI_API_KEY`` first then ``OPENAI_API_KEY`` — matching the
  in-container precedence of
  :func:`runtimes._openai_sdk_common.provision_openai_auth` (which
  bridges ``SAC_OPENAI_API_KEY`` → ``OPENAI_API_KEY`` for the SDK).
* ``OPENAI_API_KEY`` ← the same value, injected directly as well so
  in-container consumers WITHOUT the sac bridge (raw ``openai`` client
  usage by tools, future non-runner processes) authenticate too — the
  same dual-injection shape the Anthropic-compat path above uses for
  ``ANTHROPIC_API_KEY``.
* ``SAC_PROVIDER=openai`` ← the resolved harness, made observable
  in-container (forward wiring for in-container runner selection).
* ``OPENAI_BASE_URL`` / ``OPENAI_ORG_ID`` / ``OPENAI_PROJECT_ID`` /
  ``SAC_OPENAI_MODEL`` ← forwarded verbatim from the host env when set
  (optional routing/attribution knobs; a host export would otherwise
  silently do nothing for containerized agents).

Fail-loud, same doctrine as the Anthropic-compat path: no resolvable
key → :class:`ProviderEnvError` (a silent fallback would boot an agent
whose every turn 401s behind a fresh-looking heartbeat); composing
``spec.harness: openai`` with an active ``spec.claude.provider``
backend override → :class:`ProviderEnvError` (the nested override
configures the Claude SDK, which an ``openai``-harness agent never runs).
"""

from __future__ import annotations

import os
from pathlib import Path

from scitex_config import PriorityConfig, load_dotenv

from ..config import AgentConfig
from ..config._harness_registry import known_harnesses
from ._apptainer_context_window import context_window_env
from ._apptainer_provider_cfg import container_config_dir


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


def resolve_provider_api_key(config: AgentConfig) -> str:
    """Resolve the provider API key VALUE for ``config`` (fail-loud).

    Shared by :func:`provider_env_flags` (which injects it into the
    container env) and :mod:`._apptainer_provider_cfg` (which pre-approves
    it in the config dir), so the two can never disagree about which key
    the agent runs on. Raises :class:`ProviderEnvError` when
    ``provider.auth_token_env`` is empty or resolves to nothing after the
    scitex-config cascade (see module docstring). The value is never
    logged by sac.
    """
    provider = _provider_spec(config)
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
    return api_key


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
    api_key = resolve_provider_api_key(config)

    # Per-agent clean config dir — the conflict-breaker. Distinct from the
    # OAuth path's /tmp/sac-claude so a stale OAuth bind can never win.
    config_dir = container_config_dir(config.name)
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


# ---------------------------------------------------------------------------
# Per-ENGINE parameters (spec.engines.<key>.{reasoning_effort,
# max_context_tokens}) — operator answer Q4, 2026-09-03.
# ---------------------------------------------------------------------------

#: Env var carrying the ENGINE KEY this container was started on. Pure
#: provenance: an operator inside the container can read what backend
#: the agent was launched against without going back to the spec.
ENGINE_KEY_ENV = "SAC_ENGINE"

#: Env vars carrying the per-engine parameters into the container.
#: NAMESPACED under ``SAC_`` deliberately — see ``engine_env_flags``.
ENGINE_REASONING_EFFORT_ENV = "SAC_ENGINE_REASONING_EFFORT"
ENGINE_MAX_CONTEXT_TOKENS_ENV = "SAC_ENGINE_MAX_CONTEXT_TOKENS"

#: The Claude-family harness's OWN name for the context window, and the one
#: mapping in this module that is MEASURED rather than assumed (2026-09-05).
#: Claude Code assumes 200,000 tokens for a model name it does not recognise
#: and auto-compacts at that boundary; its own notice says so and names this
#: variable as the fix ("set CLAUDE_CODE_MAX_CONTEXT_TOKENS to its real
#: window ... Until then auto-compact keeps this session within <N> tokens
#: (the context window it assumes)") -- read out of the 2.1.258 binary the
#: agent image ships. Effect measured on the live fleet the same day: agent
#: `business` on qwen38-27b (served window 1048576) read ctx:100% and looped
#: on failed compactions; with this variable set to 1048576 the SAME pinned
#: transcript read ctx:64% and stopped compacting.
CLAUDE_CODE_MAX_CONTEXT_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def engine_env_flags(config: AgentConfig) -> list[str]:
    """Render the ``--env`` flags carrying the selected engine's parameters.

    Returns ``[]`` for a config with no engine selected and no
    parameters — every legacy single-backend spec, so the launch argv is
    byte-identical to what it was before ``spec.engines`` existed.

    WHAT THIS DELIVERS, AND WHAT IT DOES NOT. sac's contract stops at
    putting the declaration inside the container under a name that says
    where it came from. Whether the in-container harness ACTS on
    ``SAC_ENGINE_REASONING_EFFORT`` is the harness's business, and sac
    does not claim otherwise — which is why these are ``SAC_``-prefixed
    rather than dressed up as a vendor env var (``MAX_THINKING_TOKENS``,
    say) that would imply a mapping nobody has measured. Saying so here
    is the point: a field that VALIDATES is not a field that RUNS, and a
    green test on this function proves delivery, not effect.

    ONE MAPPING IS NOW MEASURED, and only one. ``max_context_tokens``
    ALSO renders the Claude-family harness's own variable
    (:data:`CLAUDE_CODE_MAX_CONTEXT_ENV`) when the launch resolves to the
    ``anthropic`` harness -- see that constant for the binary text and the
    fleet measurement behind it. ``reasoning_effort`` keeps its
    ``SAC_``-only delivery: no equivalent measurement exists for it yet,
    and inventing one would be the silent claim this codebase keeps
    paying for. The openai / codex harnesses get the ``SAC_`` name only;
    a Codex mapping belongs to whoever measures Codex.

    The engine's own ``env:`` map is NOT rendered here — it is merged
    into ``config.env`` by ``config._engine_types.apply_engine`` and
    reaches the container through the normal ``effective_env`` path, so
    an operator can spell a harness's real knob today without waiting
    for sac to model it.
    """
    flags: list[str] = []
    key = str(getattr(config, "engine_key", "") or "").strip()
    if key:
        flags += ["--env", f"{ENGINE_KEY_ENV}={key}"]
    effort = str(getattr(config, "reasoning_effort", "") or "").strip()
    if effort:
        flags += ["--env", f"{ENGINE_REASONING_EFFORT_ENV}={effort}"]
    max_ctx = getattr(config, "max_context_tokens", None)
    if max_ctx:
        flags += ["--env", f"{ENGINE_MAX_CONTEXT_TOKENS_ENV}={int(max_ctx)}"]
        # ASK THE HARNESS, DO NOT TEST FOR A VENDOR. This used to read
        # ``if resolve_agent_harness(config) == DEFAULT_AGENT_HARNESS``,
        # which handed the engine's declared window to ONE program and
        # silently dropped it for every other — a privilege granted by an
        # ``if``, not by a measurement. Each descriptor now spells its own
        # context-window variable (``context_window_env``), and a harness
        # that takes its window some other way answers ``None`` explicitly
        # (codex renders ``-c model_context_window`` onto the argv
        # instead). Adding a harness that needs an env var is one registry
        # column, not an edit here.
        context_env = context_window_env(config, resolve_agent_harness(config))
        if context_env:
            flags += ["--env", f"{context_env}={int(max_ctx)}"]
    return flags


# ---------------------------------------------------------------------------
# OpenAI harness columns (spec.harness — the TOP-LEVEL axis).
# See the "OpenAI harness columns" section of the module docstring.
# ---------------------------------------------------------------------------

# OPS-ONLY override for the harness axis. NOT a spec surface: specs
# declare ``spec.harness``; this host env var overrides it for every
# launch in the exporting shell (emergency flips, A/B smoke tests)
# without editing specs. Documented in docs/spec-reference.md.
#
# The env var keeps its ``SAC_PROVIDER`` name deliberately: it is an
# operator-facing surface with its own migration cost, and renaming it in
# the same change that migrates the spec key would flip two surfaces at
# once. It is listed as a follow-up, not fixed here.
AGENT_HARNESS_ENV = "SAC_PROVIDER"

# DERIVED from the harness registry (v4 step 4): the families the
# descriptor entries declare, so a new harness is one registry entry.
_VALID_AGENT_HARNESSES = known_harnesses()

_SAC_OPENAI_KEY_ENV = "SAC_OPENAI_API_KEY"
_OPENAI_KEY_ENV = "OPENAI_API_KEY"

# Optional OpenAI routing/attribution knobs forwarded host → container
# verbatim when set. No fail-loud — each merely refines client routing
# (gateway base URL, org/project attribution, default model); absence
# means "the SDK default". Forwarded because apptainer isolates the env:
# a host-side export would otherwise silently do nothing in-container.
_OPENAI_PASSTHROUGH_ENVS = (
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "SAC_OPENAI_MODEL",
)


def resolve_agent_harness(config: AgentConfig) -> str:
    """Resolve the harness axis (``spec.harness``) for one launch.

    Precedence: ``$SAC_PROVIDER`` (host env, OPS-ONLY override) →
    ``spec.harness`` (or its deprecated ``spec.provider`` alias, already
    resolved by the loader) → ``"anthropic"`` (the default harness).

    ``SAC_PROVIDER`` is deliberately an operations escape hatch, not a
    config surface: it flips EVERY agent launched from the exporting
    shell (blast radius = the shell, mirroring
    ``SAC_QUOTA_CACHE_HOST_PATH``), which is exactly what an emergency
    fleet flip or an A/B smoke test wants and exactly what a per-agent
    spec should never rely on. Persistent per-agent selection belongs in
    ``spec.harness``.

    Raises :class:`ProviderEnvError` when ``SAC_PROVIDER`` carries an
    unknown harness — a typo must not silently launch the default.
    """
    override = os.environ.get(AGENT_HARNESS_ENV, "").strip().lower()
    if override:
        if override not in _VALID_AGENT_HARNESSES:
            valid = ", ".join(_VALID_AGENT_HARNESSES)
            raise ProviderEnvError(
                f"${AGENT_HARNESS_ENV}={override!r} is not a known agent "
                f"harness (valid: {valid}). Unset it or set one of the "
                "valid harnesses — refusing to guess."
            )
        return override
    harness = str(getattr(config, "harness", "") or "").strip().lower()
    return harness or "anthropic"


def openai_harness_active(config: AgentConfig) -> bool:
    """True when this launch resolves to the ``openai`` harness."""
    return resolve_agent_harness(config) == "openai"


def openai_env_flags(config: AgentConfig) -> list[str]:
    """Render the ``--env`` flags for an ``openai``-harness agent.

    Returns ``[]`` when the launch does not resolve to the ``openai``
    harness. Raises :class:`ProviderEnvError` (fail-loud) when:

    * an Anthropic-compat ``spec.claude.provider`` backend override is
      ALSO active (that override configures the Claude SDK, which an
      ``openai``-harness agent never runs — the composition is a config
      error, not a preference), or
    * no API key resolves through the scitex-config cascade (tried as
      ``SAC_OPENAI_API_KEY`` first, then ``OPENAI_API_KEY`` — the same
      precedence :func:`~runtimes._openai_sdk_common.provision_openai_auth`
      applies in-container).

    The key VALUE is embedded in the argv but never logged by sac
    (``PriorityConfig`` masks it in its resolution log). See the module
    docstring for the full flag inventory.
    """
    if not openai_harness_active(config):
        return []

    if provider_active(config):
        raise ProviderEnvError(
            "spec.harness: openai cannot compose with an active "
            "spec.claude.provider backend override — the nested override "
            "points the CLAUDE SDK at an Anthropic-compatible gateway, "
            "which an openai-harness agent never runs. Remove one of the "
            "two declarations."
        )

    # Same scitex-config precedence as provider_env_flags above:
    # shell-export > $HOME/.env > default (see that function's comment
    # for why the dotenv path is pinned to $HOME/.env).
    load_dotenv(dotenv_path=str(Path.home() / ".env"))
    resolver = PriorityConfig(auto_uppercase=False)
    api_key = resolver.resolve(key=_SAC_OPENAI_KEY_ENV, default="")
    if not api_key:
        api_key = resolver.resolve(key=_OPENAI_KEY_ENV, default="")
    if not api_key:
        raise ProviderEnvError(
            f"spec.harness: openai but neither {_SAC_OPENAI_KEY_ENV} "
            f"(preferred; sac-tracked) nor {_OPENAI_KEY_ENV} resolves "
            "through scitex-config (shell export > $HOME/.env). Set the "
            "key by EITHER exporting it in the shell that runs `sac "
            f"agents start` OR adding a `{_SAC_OPENAI_KEY_ENV}=...` line "
            "to $HOME/.env (chmod 0600). sac reads the value at start "
            "and never logs it."
        )

    flags = [
        # Dual injection, mirroring the Anthropic-compat path above:
        # SAC_OPENAI_API_KEY is the sac-tracked handoff (bridged to
        # OPENAI_API_KEY in-container by provision_openai_auth, with
        # provenance preserved); OPENAI_API_KEY directly serves any
        # in-container consumer without the sac bridge.
        "--env",
        f"{_SAC_OPENAI_KEY_ENV}={api_key}",
        "--env",
        f"{_OPENAI_KEY_ENV}={api_key}",
        # Make the resolved harness observable in-container (forward
        # wiring for in-container runner/executor selection).
        "--env",
        f"{AGENT_HARNESS_ENV}=openai",
    ]
    for env_name in _OPENAI_PASSTHROUGH_ENVS:
        val = os.environ.get(env_name, "")
        if val:
            flags.extend(["--env", f"{env_name}={val}"])
    return flags


__all__ = [
    "AGENT_HARNESS_ENV",
    "ENGINE_KEY_ENV",
    "ENGINE_MAX_CONTEXT_TOKENS_ENV",
    "ENGINE_REASONING_EFFORT_ENV",
    "ProviderEnvError",
    "engine_env_flags",
    "openai_env_flags",
    "openai_harness_active",
    "provider_active",
    "provider_env_flags",
    "resolve_provider_api_key",
    "resolve_agent_harness",
]
