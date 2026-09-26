"""Neutral paid-provider egress gateway resolved per launch host.

SAC only points a harness at an authenticated endpoint. Model allowlists,
vendor credentials, accounting, and spend policy belong to scitex-genai at
the outbound boundary. In particular, an agent container must never receive
``DEEPSEEK_API_KEY`` merely because its selected model is DeepSeek.
"""

from __future__ import annotations

import os

EXTERNAL_GATEWAY_PROVIDER = "external-gateway"
EXTERNAL_GATEWAY_HOST = "scitex-compute-04"
EXTERNAL_GATEWAY_PORT = 18775
DEFAULT_EXTERNAL_GATEWAY_URL = (
    f"http://{EXTERNAL_GATEWAY_HOST}:{EXTERNAL_GATEWAY_PORT}"
)
DEFAULT_EXTERNAL_GATEWAY_TOKEN_ENV = "SCITEX_GENAI_GATEWAY_API_KEY"
EXTERNAL_GATEWAY_URL_ENV = "SAC_EXTERNAL_GATEWAY_URL"
EXTERNAL_GATEWAY_TOKEN_ENV_ENV = "SAC_EXTERNAL_GATEWAY_TOKEN_ENV"


def external_gateway_url() -> str:
    return (os.environ.get(EXTERNAL_GATEWAY_URL_ENV) or "").strip() or (
        DEFAULT_EXTERNAL_GATEWAY_URL
    )


def external_gateway_token_env() -> str:
    return (os.environ.get(EXTERNAL_GATEWAY_TOKEN_ENV_ENV) or "").strip() or (
        DEFAULT_EXTERNAL_GATEWAY_TOKEN_ENV
    )


def external_gateway_provider_entry() -> dict[str, str | None]:
    return {
        "base_url": external_gateway_url(),
        "auth_token_env": external_gateway_token_env(),
    }


__all__ = [
    "DEFAULT_EXTERNAL_GATEWAY_TOKEN_ENV",
    "DEFAULT_EXTERNAL_GATEWAY_URL",
    "EXTERNAL_GATEWAY_PROVIDER",
    "EXTERNAL_GATEWAY_TOKEN_ENV_ENV",
    "EXTERNAL_GATEWAY_URL_ENV",
    "external_gateway_provider_entry",
    "external_gateway_token_env",
    "external_gateway_url",
]
