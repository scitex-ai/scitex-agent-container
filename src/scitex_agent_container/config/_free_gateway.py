"""The fleet free-SKU gateway — ONE address, written once, overridable per host.

The free ``muse-spark-1.3-contributor-free`` SKU is app-locked: raw Zen API
calls 403, but the OpenCode app carries the client identity the SKU demands.
Our scitex-genai gateway fronts it: ``OpenCodeBackend`` drives local
``opencode serve :4096`` and serves OpenAI chat-completions on :18779
(``~/.scitex/genai/config-opencode-free.yaml``, systemd
``scitex-genai-free.service``). Verified 2026-09-22 on scitex-compute-04:
``/v1/models`` lists the SKU, ``/v1/chat/completions`` round-trips at cost 0.

Same shape as :mod:`._qwen_gateway`: the address lives HERE, specs write the
bare provider name. The engine model for this backend is
``muse-spark-1.3-contributor-free`` (see ``_engine_library``); the ADDRESS
stayed here because an address that must resolve differently per host is
code, not data.

OVERRIDING IT. :data:`FREE_GATEWAY_URL_ENV` overrides the address for one
host or one process, read at RESOLVE time, not import time. The API key's
value is never held here — only the NAME of the host env var holding it.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_FREE_GATEWAY_TOKEN_ENV",
    "DEFAULT_FREE_GATEWAY_URL",
    "FREE_GATEWAY_HOST",
    "FREE_GATEWAY_PORT",
    "FREE_GATEWAY_PROBE_PATH",
    "FREE_GATEWAY_PROVIDER",
    "FREE_GATEWAY_TOKEN_ENV_ENV",
    "FREE_GATEWAY_URL_ENV",
    "free_gateway_probe_url",
    "free_gateway_provider_entry",
    "free_gateway_token_env",
    "free_gateway_url",
]

#: The registered provider name a spec writes instead of an address.
FREE_GATEWAY_PROVIDER = "scitex-free"

#: The hostname that serves the free endpoint. Loopback by default: the
#: gateway runs on every host that launches free-SKU agents (systemd
#: scitex-genai-free.service). A host without a local gateway overrides
#: via SAC_FREE_GATEWAY_URL to point at a peer.
FREE_GATEWAY_HOST = "127.0.0.1"

#: The OpenAI-protocol port the free endpoint serves.
FREE_GATEWAY_PORT = 18779

#: Where the gateway is, absent an override.
DEFAULT_FREE_GATEWAY_URL = f"http://{FREE_GATEWAY_HOST}:{FREE_GATEWAY_PORT}/v1"

#: The NAME of the host env var holding the gateway key — never the key.
DEFAULT_FREE_GATEWAY_TOKEN_ENV = "SCITEX_GENAI_GATEWAY_API_KEY"

#: Per-host override for the address. Read at resolve time.
FREE_GATEWAY_URL_ENV = "SAC_FREE_GATEWAY_URL"

#: Per-host override for the env-var NAME the key is read from.
FREE_GATEWAY_TOKEN_ENV_ENV = "SAC_FREE_GATEWAY_TOKEN_ENV"

#: The path a PREFLIGHT asks for: 401 proves REACHABLE + AUTH-GATED.
FREE_GATEWAY_PROBE_PATH = "/v1/models"


def free_gateway_url() -> str:
    """The gateway address for THIS host, honouring the env override."""
    return (os.environ.get(FREE_GATEWAY_URL_ENV) or "").strip() or (
        DEFAULT_FREE_GATEWAY_URL
    )


def free_gateway_probe_url() -> str:
    """The address a reachability probe should actually dial."""
    return f"{free_gateway_url().rstrip('/')}{FREE_GATEWAY_PROBE_PATH}"


def free_gateway_token_env() -> str:
    """The NAME of the env var holding the gateway key, honouring override."""
    return (os.environ.get(FREE_GATEWAY_TOKEN_ENV_ENV) or "").strip() or (
        DEFAULT_FREE_GATEWAY_TOKEN_ENV
    )


def free_gateway_provider_entry() -> "dict[str, str | None]":
    """The registry entry for :data:`FREE_GATEWAY_PROVIDER`, resolved now."""
    return {
        "base_url": free_gateway_url(),
        "auth_token_env": free_gateway_token_env(),
    }
