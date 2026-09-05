"""``--env`` / ``--bind`` flags for a ``codex``-harness agent launch.

Card ``sac-codex-python-sdk-harness-20260814`` — the fourth harness. The
registry's ``codex-sdk`` entry points its ``env_and_binds`` hook here
(:func:`config._harness_callables._codex_env_and_binds`).

WHY THIS HARNESS BINDS A DIRECTORY INSTEAD OF INJECTING A KEY
-------------------------------------------------------------
The ``openai-agents`` entry reduces its whole auth story to
``OPENAI_API_KEY`` (see ``_apptainer_provider.openai_env_flags``). The
Codex SDK cannot: ``openai-codex`` drives the ``codex`` binary, and that
binary reads BOTH halves of its configuration out of one directory —
``$CODEX_HOME`` (default ``~/.codex``):

* ``auth.json`` — the credential the SDK reuses. Per the SDK's own
  getting-started doc, "Existing Codex authentication is reused
  automatically"; the alternatives are an interactive ChatGPT browser
  login and a device-code flow, neither of which a headless container
  can complete. An API key is a THIRD option
  (``AsyncCodex.login_api_key``), and when one is available this module
  passes it through too — but it is not the only shape.
* ``config.toml`` — ``model`` / ``model_provider`` / the
  ``[model_providers.*]`` tables carrying ``base_url`` + ``wire_api``.
  That is the ONLY surface that points codex at a non-OpenAI endpoint,
  so a launch that loses this file loses the fleet's local-model routing
  entirely and silently falls back to OpenAI-hosted models.

So the honest flag set is a bind of the directory plus a ``CODEX_HOME``
that names it, not an env var pretending the credential is a string.

THE ``codex`` NAME APPEARS ON TWO DIFFERENT AXES — refused, not guessed
-----------------------------------------------------------------------
``spec.claude.provider: codex`` (``config._provider_registry``) is an
INFERENCE backend: the local scitex-genai gateway on 127.0.0.1:18765
translating Anthropic Messages onto a ChatGPT Codex subscription, with
Claude Code still running the loop. ``spec.harness: codex`` (this
module) is a HARNESS: the codex agent program runs the loop and brings
its own file-edit / exec tooling.

They are the two axes this package split apart in #1027, and ``codex``
is the first value to appear on both. A spec that states BOTH has said
two contradictory things — "Claude Code drives, pointed at the codex
gateway" and "codex drives" — so this module refuses it loudly with
:class:`ProviderEnvError` rather than silently honouring one. That is
the #1039 rule (a spec must never validate and then launch something
other than what it declared) applied to the exact collision that makes
this harness special.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config._types import AgentConfig
from ._apptainer_provider import (
    ProviderEnvError,
    _provider_spec,
    provider_active,
    resolve_agent_harness,
    resolve_provider_api_key,
)

__all__ = [
    "CODEX_HOME_ENV",
    "codex_env_flags",
    "codex_harness_active",
    "codex_provider_key_flags",
    "container_codex_home",
    "resolve_codex_home",
]

#: The env var the ``codex`` binary reads its config+creds directory from.
CODEX_HOME_ENV = "CODEX_HOME"

#: Prefix of the path INSIDE the container the host's codex home is bound
#: to: ``/tmp/sac-<name>-codex-home``. Under ``/tmp`` — which exists in the
#: image — because apptainer refuses a bind whose DESTINATION is absent
#: ("destination /home/agent/.codex doesn't exist in container", measured
#: on the first live codex start, handyman-01, 2026-09-05 08:59 UTC), and
#: per agent so two codex agents on one host never share a session store.
#: The same shape ``_apptainer_provider_cfg.container_config_dir`` uses.
CONTAINER_CODEX_HOME_PREFIX = "/tmp/sac-"


def container_codex_home(name: str) -> str:
    """The in-container ``CODEX_HOME`` for agent ``name``."""
    return f"{CONTAINER_CODEX_HOME_PREFIX}{name}-codex-home"


#: API-key env vars, in resolution order. ``SAC_CODEX_API_KEY`` is sac's
#: own override; ``CODEX_API_KEY`` is the binary's; ``OPENAI_API_KEY`` is
#: the fallback the codex CLI itself honours for OpenAI-hosted models.
_KEY_ENVS = ("SAC_CODEX_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY")

#: Routing pass-throughs — the sac-namespaced env the in-container runner
#: reads (see ``_runners._codex_options``) to pick the model, the
#: ``[model_providers.*]`` entry and the sandbox. Forwarded when set on
#: the host, skipped when not, so the container's env stays minimal.
#:
#: These are the surface that points a codex agent at a SELF-HOSTED
#: endpoint, and without forwarding them the harness would come up
#: hardwired to codex's OpenAI-hosted default with no way to say
#: otherwise short of hand-editing the bound config.toml. There is no
#: ``spec.codex`` block yet (that needs a typed spec section + validation
#: — a follow-up); until there is, an operator sets these in the launching
#: shell or in ``spec.apptainer.env``.
_ROUTING_ENVS = (
    "SAC_CODEX_MODEL",
    "SAC_CODEX_MODEL_PROVIDER",
    "SAC_CODEX_SANDBOX",
)


def _is_registry_codex_backend(config: AgentConfig) -> bool:
    """True when the active provider IS ``spec.claude.provider: codex``.

    That named backend is the scitex-genai gateway translating Anthropic
    Messages to a ChatGPT Codex subscription with Claude Code driving —
    the two-axis collision the docstring above describes. An INLINE
    provider (``base_url`` + ``auth_token_env``, the engines surface) is
    the opposite case: it is the inference endpoint the codex harness
    itself is pointed at (2026-09-05), so it composes.
    """
    from ..config._provider_registry import resolve_provider

    named = resolve_provider("codex")
    active = _provider_spec(config)
    if named is None or active is None:
        return False
    named_url = (
        named.get("base_url", "")
        if isinstance(named, dict)
        else getattr(named, "base_url", "")
    )
    return str(getattr(active, "base_url", "")).rstrip("/") == str(named_url).rstrip(
        "/"
    )


def codex_provider_key_flags(config: AgentConfig) -> list[str]:
    """``--env SAC_CODEX_API_KEY=<key>`` from the engine's provider block.

    The rendered Codex config names this env var as the provider's
    ``env_key`` (``_apptainer_inner_argv_codex.CODEX_KEY_ENV``); the value
    is the same resolved key the Claude path would put in
    ANTHROPIC_API_KEY. Empty when no inline provider is active — then the
    binary's own key names (``_KEY_ENVS``) are all there is.
    """
    if not codex_harness_active(config) or not provider_active(config):
        return []
    if _is_registry_codex_backend(config):
        return []
    from ._apptainer_inner_argv_codex import CODEX_KEY_ENV

    return ["--env", f"{CODEX_KEY_ENV}={resolve_provider_api_key(config)}"]


def codex_harness_active(config: AgentConfig) -> bool:
    """True when this launch resolves to the ``codex`` harness."""
    return resolve_agent_harness(config) == "codex"


def resolve_codex_home(state_dir: Path | None = None) -> Path:
    """The HOST directory holding codex's config, auth and session rollouts.

    ``$CODEX_HOME`` when exported (the binary's own override — honoured
    so an operator who already relocated it does not have to say so
    twice), else the agent's own ``<state_dir>/codex-home`` (2026-09-05:
    per agent, like the provider config dir, so sessions never mix), else
    ``~/.codex`` when no state dir is known.
    """
    override = os.environ.get(CODEX_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if state_dir is not None:
        return Path(state_dir).expanduser() / "codex-home"
    return Path.home() / ".codex"


def codex_env_flags(config: AgentConfig, state_dir: Path) -> list[str]:
    """Render the ``--bind`` / ``--env`` flags for a ``codex``-harness agent.

    Returns ``[]`` when the launch does not resolve to the ``codex``
    harness — the same decline-quietly contract every other
    ``env_and_binds`` hook honours, so a Claude launch is byte-identical
    whether or not this module exists.

    Raises :class:`ProviderEnvError` (fail-loud) when a
    ``spec.claude.provider`` backend override is ALSO active: that
    override configures the CLAUDE SDK, which a codex-harness agent
    never runs. The composition is a config error, not a preference —
    and with ``provider: codex`` it is the specific two-axis collision
    described in the module docstring.

    Does NOT raise when no credential is found. Unlike the openai
    harness — where a missing key means the very first request 401s —
    codex has three auth shapes and only one of them is an env var; a
    bound ``auth.json`` is both the documented default and invisible to
    this function (the file lives on the host and may be created between
    render and launch). The runner raises the actionable error at
    ``session.start()`` instead, where it can see the real answer.
    """
    if not codex_harness_active(config):
        return []

    if provider_active(config) and _is_registry_codex_backend(config):
        raise ProviderEnvError(
            "spec.harness: codex cannot compose with an active "
            "spec.claude.provider backend override — the nested override "
            "points the CLAUDE SDK at an Anthropic-compatible gateway, "
            "which a codex-harness agent never runs. Note these are two "
            "DIFFERENT axes that share the word 'codex': "
            "spec.claude.provider: codex is an INFERENCE backend (the "
            "scitex-genai gateway, Claude Code still driving), while "
            "spec.harness: codex replaces the HARNESS with the codex "
            "agent program. Stating both says two contradictory things "
            "about who runs the loop. Remove one of the two declarations."
        )

    codex_home = resolve_codex_home(state_dir)
    # The bind SOURCE must exist or apptainer refuses the whole container
    # ("mount source ... no such file or directory", measured on the first
    # live codex start, handyman-01, 2026-09-05 08:57 UTC: the host had never
    # run codex, so the directory was absent and the pane died before boot).
    # This is the directory codex itself would create on first run; sac
    # creates it, private to the user, so a fresh host starts like a used one.
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    inside = container_codex_home(config.name)
    argv: list[str] = [
        "--bind",
        f"{codex_home}:{inside}",
        "--env",
        f"{CODEX_HOME_ENV}={inside}",
    ]

    for env_name in _KEY_ENVS:
        value = os.environ.get(env_name, "").strip()
        if value:
            # Passed under the binary's OWN name regardless of which
            # alias supplied it, so the container needs no sac knowledge.
            argv += ["--env", f"CODEX_API_KEY={value}"]
            break

    for env_name in _ROUTING_ENVS:
        value = os.environ.get(env_name, "").strip()
        if value:
            argv += ["--env", f"{env_name}={value}"]

    return argv
