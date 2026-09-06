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

ONE ENTRY IS RESOLVED, NOT READ — see :data:`_DYNAMIC_PROVIDERS`. The
fleet Qwen gateway moves with the host that serves it, so its address is
owned by :mod:`._qwen_gateway` and overridable from the environment. Its
row in :data:`PROVIDERS` below is the DEFAULT, kept there so
:func:`list_providers` names it and the shape of the table stays uniform;
every read must go through :func:`resolve_provider`, which applies the
override. Reading :data:`PROVIDERS` directly would silently ignore a
per-host override — no caller in this package does.
"""

from __future__ import annotations

from ._qwen_gateway import (
    DEFAULT_QWEN_GATEWAY_TOKEN_ENV,
    DEFAULT_QWEN_GATEWAY_URL,
    QWEN_GATEWAY_PROVIDER,
    qwen_gateway_provider_entry,
)

PROVIDERS: dict[str, dict[str, str | None]] = {
    "anthropic": {
        "base_url": None,
        "auth_token_env": None,
    },
    # Local scitex-genai bridge: Claude Code remains the harness while the
    # gateway translates Anthropic Messages to the ChatGPT Codex transport.
    # Account discovery, stickiness, quota ranking, and failover live in the
    # gateway; sac only points the harness at its authenticated endpoint.
    "codex": {
        "base_url": "http://127.0.0.1:18765",
        "auth_token_env": "SCITEX_GENAI_GATEWAY_API_KEY",
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
    # The fleet Qwen gateway. THE DEFAULT ONLY — resolved through
    # _DYNAMIC_PROVIDERS below so $SAC_QWEN_GATEWAY_URL wins on a host that
    # reaches it another way. Written into every migrated spec as the bare
    # name `qwen-gateway`, so the address is in this file and not in 119
    # spec.yaml files.
    QWEN_GATEWAY_PROVIDER: {
        "base_url": DEFAULT_QWEN_GATEWAY_URL,
        "auth_token_env": DEFAULT_QWEN_GATEWAY_TOKEN_ENV,
    },
}

#: Providers whose metadata is HOST-DEPENDENT and therefore computed on each
#: read instead of frozen in :data:`PROVIDERS`. Keyed by provider name, valued
#: by a zero-argument callable returning the same two-field dict.
#:
#: A frozen constant cannot honour an environment variable exported after
#: import, and sac is imported long before a launch resolves a provider. One
#: member today; the mechanism is general so a second movable backend does not
#: become a second special case in :func:`resolve_provider`.
_DYNAMIC_PROVIDERS = {QWEN_GATEWAY_PROVIDER: qwen_gateway_provider_entry}


def resolve_provider(name: str) -> dict[str, str | None] | None:
    """Return ``{base_url, auth_token_env}`` for a registered provider.

    Returns ``None`` when ``name`` is not in :data:`PROVIDERS`. Callers
    that need a fail-loud surface (e.g. the spec validator) should
    detect ``None`` and emit a clear error naming the known providers
    via :func:`list_providers`.

    The returned dict is a copy — callers may mutate it without
    disturbing the registry.

    A name in :data:`_DYNAMIC_PROVIDERS` is RESOLVED here rather than read
    from :data:`PROVIDERS`, so a per-host override is honoured by every
    caller (the parser, the validator, the honourability verdict and the
    launch argv all arrive through this one function).
    """
    if name not in PROVIDERS:
        return None
    dynamic = _DYNAMIC_PROVIDERS.get(name)
    if dynamic is not None:
        return dict(dynamic())
    return dict(PROVIDERS[name])


def list_providers() -> list[str]:
    """Return the registered provider names, sorted, for diagnostics.

    Used by the spec validator's "unknown provider" error message so
    the operator sees the exact set they can pick from without having
    to read the module source.
    """
    return sorted(PROVIDERS)


# The HARNESS registry (``spec.harness`` — which agent SDK runs the
# session) used to live here as ``AGENT_SDK_PROVIDERS``, a second flat
# constant sharing this module's "provider" word with :data:`PROVIDERS`
# while meaning something unrelated. It now lives in
# ``config._harness_types`` as ``AGENT_HARNESSES``; this module is the
# INFERENCE-backend registry only.

__all__ = [
    "PROVIDERS",
    "resolve_provider",
    "list_providers",
]
