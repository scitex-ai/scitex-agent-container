"""Provider registry — string-identifier → backend metadata.

Operator directive 2026-05-28 (Telegram msg 6783): simplify
``spec.claude.provider`` from a ``{base_url, auth_token_env}`` dict to
a bare string identifier (``provider: mimo``). sac resolves the
backend metadata (Anthropic-compatible base URL + the NAME of the host
env var holding the API key) internally from this registry.

The dict shape stays accepted for back-compat (see
``_parsers._claude._parse_provider`` and ``_validation._validate_provider``);
existing agent specs that declare ``provider: {base_url, auth_token_env}``
continue to load and run identically. Operators migrating to the new
shape just replace the dict with the registered name.

Adding a new backend: add one entry to :data:`PROVIDERS`. Each entry is
``{base_url, auth_token_env}`` — the same two fields the dict shape
exposes — so a string-form spec is byte-equivalent to a dict-form spec
that copy-pasted the registry entry verbatim.

The ``"anthropic"`` entry is intentional: it lets an operator write
``provider: anthropic`` to spell out "use the default Anthropic OAuth
backend" explicitly without having to omit the field. The runtime
treats an entry with ``base_url=None`` as "no backend override", same
as no provider at all.

Aliases (e.g. ``"xiaomi"`` → same metadata as ``"mimo"``) are
intentional duplicates rather than indirection: the registry stays a
flat read so no caller has to chase aliases.
"""

from __future__ import annotations

PROVIDERS: dict[str, dict[str, str | None]] = {
    "anthropic": {
        "base_url": None,
        "auth_token_env": None,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_token_env": "DEEPSEEK_API_KEY",
    },
    "mimo": {
        "base_url": "https://token-plan-sgp.xiaomimimo.com/anthropic",
        "auth_token_env": "XIAOMI_API_KEY",
    },
    "xiaomi": {
        "base_url": "https://token-plan-sgp.xiaomimimo.com/anthropic",
        "auth_token_env": "XIAOMI_API_KEY",
    },
}


def resolve_provider(name: str) -> dict[str, str | None] | None:
    """Return ``{base_url, auth_token_env}`` for a registered provider.

    Returns ``None`` when ``name`` is not in :data:`PROVIDERS`. Callers
    that need a fail-loud surface (e.g. the spec validator) should
    detect ``None`` and emit a clear error naming the known providers
    via :func:`list_providers`.

    The returned dict is a copy — callers may mutate it without
    disturbing the registry.
    """
    entry = PROVIDERS.get(name)
    if entry is None:
        return None
    return dict(entry)


def list_providers() -> list[str]:
    """Return the registered provider names, sorted, for diagnostics.

    Used by the spec validator's "unknown provider" error message so
    the operator sees the exact set they can pick from without having
    to read the module source.
    """
    return sorted(PROVIDERS)


# ---------------------------------------------------------------------------
# Agent SDK family registry — spec.provider (TOP-LEVEL; openai-compat-1
# foundation)
# ---------------------------------------------------------------------------
#
# Deliberately a SEPARATE, flat constant from :data:`PROVIDERS` above:
# ``PROVIDERS`` resolves a vendor BACKEND (base_url + auth_token_env) that
# the Claude Agent SDK talks to; :data:`AGENT_SDK_PROVIDERS` is the closed
# set of AGENT SDK FAMILIES sac knows how to run a session through at all
# (see the naming-collision note in ``config._provider_types.AgentProvider``
# for why these are two different axes sharing an unfortunately similar
# name). Landed foundation-only — ``"openai"`` validates but has no runner
# implementation until openai-compat-2.
AGENT_SDK_PROVIDERS: tuple[str, ...] = ("anthropic", "openai")


def is_known_agent_provider(name: str) -> bool:
    """True when ``name`` is a recognized ``spec.provider`` SDK family."""
    return name in AGENT_SDK_PROVIDERS


def list_agent_providers() -> list[str]:
    """Return the recognized ``spec.provider`` SDK families, sorted.

    Used by the spec validator's "unknown provider" error message —
    mirrors :func:`list_providers` for the (distinct) vendor-backend axis.
    """
    return sorted(AGENT_SDK_PROVIDERS)


__all__ = [
    "PROVIDERS",
    "resolve_provider",
    "list_providers",
    "AGENT_SDK_PROVIDERS",
    "is_known_agent_provider",
    "list_agent_providers",
]
