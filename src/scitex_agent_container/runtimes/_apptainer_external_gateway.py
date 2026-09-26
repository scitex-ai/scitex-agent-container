"""Final Apptainer credential boundary for the neutral external gateway."""

from __future__ import annotations

from typing import Any

from ..config._external_gateway import external_gateway_url
from ._apptainer_env_dedup import env_pair_at

VENDOR_CREDENTIAL_ENV = "DEEPSEEK_API_KEY"


def external_gateway_active(config: Any) -> bool:
    """Whether the resolved inference endpoint is SAC's external gateway."""
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None) if claude is not None else None
    active_url = str(getattr(provider, "base_url", "") or "").rstrip("/")
    return bool(active_url and active_url == external_gateway_url().rstrip("/"))


def remove_vendor_credential_flags(argv: list[str], config: Any) -> list[str]:
    """Remove explicit vendor-key values before secret-file materialization."""
    if not external_gateway_active(config):
        return list(argv)
    kept: list[str] = []
    index = 0
    while index < len(argv):
        found = env_pair_at(argv, index)
        if found is not None:
            key, _value, width = found
            if key.upper() == VENDOR_CREDENTIAL_ENV:
                index += width
                continue
            kept.extend(argv[index : index + width])
            index += width
            continue
        kept.append(argv[index])
        index += 1
    return kept


def vendor_credential_denial_flags(config: Any) -> list[str]:
    """Override inherited and env-file vendor credentials with an empty value."""
    if not external_gateway_active(config):
        return []
    return ["--env", f"{VENDOR_CREDENTIAL_ENV}="]


__all__ = [
    "VENDOR_CREDENTIAL_ENV",
    "external_gateway_active",
    "remove_vendor_credential_flags",
    "vendor_credential_denial_flags",
]
