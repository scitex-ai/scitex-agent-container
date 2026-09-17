"""Resolve the runtime identity shown by agent-list consumers.

Running rows prefer the immutable, redacted birth certificate.  A current
``spec.yaml`` is only a labelled fallback: it can change after launch and an
explicit ``--engine`` selection never edits it.  The returned shape is safe for
human/JSON/browser surfaces and contains source names only, never credentials.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

IDENTITY_FIELDS = (
    "runtime",
    "harness",
    "engine",
    "model",
    "billing_mode",
    "auth_identity",
    "runtime_identity_source",
)


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _config_snapshot(config: Any) -> Mapping[str, Any] | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return dataclasses.asdict(config)
    return {
        "runtime": getattr(config, "runtime", ""),
        "harness": getattr(config, "harness", ""),
        "engine_key": getattr(config, "engine_key", ""),
        "model": getattr(config, "model", ""),
        "subscription_provider": getattr(config, "subscription_provider", ""),
        "subscription_account": getattr(config, "subscription_account", ""),
        "claude": getattr(config, "claude", None),
    }


def _birth_snapshot(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    payload = record.get("compiled_spec_json")
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _subscription_identity(snapshot: Mapping[str, Any]) -> tuple[str, str] | None:
    provider = _text(snapshot.get("subscription_provider"))
    account = _text(snapshot.get("subscription_account"))
    if not provider or not account:
        return None
    prefix, separator, unqualified = account.partition(":")
    if separator and prefix == provider and unqualified:
        account = unqualified
    return provider, account


def _provider(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    claude = _mapping(snapshot.get("claude"))
    provider = claude.get("provider")
    if not isinstance(provider, Mapping):
        provider = snapshot.get("provider")
    return _mapping(provider)


def _billing_mode(snapshot: Mapping[str, Any]) -> str:
    # Only an explicit declaration may say "usage".  An API-key source is an
    # authentication fact, not a billing contract, so it deliberately does not
    # affect this value.
    explicit = _text(snapshot.get("billing_mode") or snapshot.get("billing")).lower()
    if explicit in {"subscription", "usage", "unspecified"}:
        return explicit
    if _subscription_identity(snapshot) is not None:
        return "subscription"
    return "unspecified"


def _auth_identity(snapshot: Mapping[str, Any]) -> str:
    subscription = _subscription_identity(snapshot)
    if subscription is not None:
        return f"{subscription[0]}/{subscription[1]}"
    auth_env = _text(_provider(snapshot).get("auth_token_env"))
    if auth_env:
        return f"api-key:{auth_env}"
    claude = _mapping(snapshot.get("claude"))
    account = _text(claude.get("account"))
    if not account:
        credentials_file = _text(claude.get("credentials_file"))
        if credentials_file:
            account = PurePath(credentials_file).parent.name
    if account:
        return f"claude-code:{account}"
    return "unknown"


def _resolved(snapshot: Mapping[str, Any] | None, source: str) -> dict[str, str]:
    if snapshot is None:
        return {
            "runtime": "unknown",
            "harness": "unknown",
            "engine": "unknown",
            "model": "unknown",
            "billing_mode": "unspecified",
            "auth_identity": "unknown",
            "runtime_identity_source": "unknown",
        }
    return {
        "runtime": _text(snapshot.get("runtime")) or "unknown",
        "harness": _text(snapshot.get("harness")) or "unknown",
        "engine": _text(snapshot.get("engine_key") or snapshot.get("engine"))
        or "unknown",
        "model": _text(snapshot.get("model")) or "unknown",
        "billing_mode": _billing_mode(snapshot),
        "auth_identity": _auth_identity(snapshot),
        "runtime_identity_source": source,
    }


def resolve_runtime_identity(
    config: Any,
    *,
    running: bool,
    birth_record: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return honest Billing/Auth/Harness/Engine/Model fields and provenance."""
    if running:
        birth = _birth_snapshot(birth_record)
        if birth is not None:
            return _resolved(birth, "birth_certificate")
    spec = _config_snapshot(config)
    return _resolved(spec, "spec" if spec is not None else "unknown")


__all__ = ["IDENTITY_FIELDS", "resolve_runtime_identity"]
