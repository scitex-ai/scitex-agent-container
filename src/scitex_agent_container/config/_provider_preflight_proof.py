"""Immutable broker-to-child proof for one resolved agent configuration.

The host listener resolves a spec before selecting a provider secret. The child
CLI loads the spec again. This module binds those two loads without carrying
secret values: the listener places a SHA-256 proof of the fully resolved
``AgentConfig`` in a reserved environment variable and the child consumes it
immediately after its canonical load. A changed or newly-created spec therefore
refuses before lifecycle side effects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ._types import AgentConfig

PROVIDER_PREFLIGHT_PROOF_ENV = "SAC_PROVIDER_PREFLIGHT_PROOF_V1"
_PROOF_VERSION = "v1"


class ProviderPreflightProofError(ValueError):
    """A value-free proof refusal safe to surface to operators."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _canonical(value: Any) -> Any:
    """Return a deterministic JSON-safe representation of resolved config."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ProviderPreflightProofError("provider_config_unserializable")


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def provider_preflight_proof(config: AgentConfig) -> str:
    """Return a versioned digest of the fully resolved agent configuration."""
    return f"{_PROOF_VERSION}:config:{_digest({'config': config})}"


def absent_provider_preflight_proof(name: str) -> str:
    """Bind a failed/absent preflight so later creation cannot gain authority."""
    return f"{_PROOF_VERSION}:absent:{_digest({'name': str(name)})}"


def consume_provider_preflight_proof(
    config: AgentConfig,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> str | None:
    """Verify and remove a broker proof; direct local starts need no proof."""
    target = os.environ if environ is None else environ
    expected = target.pop(PROVIDER_PREFLIGHT_PROOF_ENV, None)
    if expected is None:
        return None
    assert_provider_preflight_proof(config, str(expected))
    return str(expected)


def assert_provider_preflight_proof(config: AgentConfig, expected: str) -> None:
    """Refuse when ``config`` differs from one immutable listener proof."""
    observed = provider_preflight_proof(config)
    if not hmac.compare_digest(expected, observed):
        raise ProviderPreflightProofError("provider_config_mismatch")


__all__ = [
    "PROVIDER_PREFLIGHT_PROOF_ENV",
    "ProviderPreflightProofError",
    "assert_provider_preflight_proof",
    "absent_provider_preflight_proof",
    "consume_provider_preflight_proof",
    "provider_preflight_proof",
]
