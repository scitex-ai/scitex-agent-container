"""Value-free immutable proof for listen-brokered lifecycle children.

The listener loads and authorizes an agent's effective provider configuration,
then starts a fresh ``sac`` process.  A pathname is not a handoff: the spec can
change between those two loads.  This module binds the listener's exact source
inode/bytes and provider-relevant effective fields into a SHA-256 proof.  Only
the versioned digest crosses the process boundary; no credential value does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from ..config import AgentConfig

BROKERED_LIFECYCLE_ENV = "SAC_BROKERED_LIFECYCLE"
EXPECTED_PROVIDER_PROOF_ENV = "SAC_EXPECTED_PROVIDER_PROOF"
_PROOF_PREFIX = "v1:sha256:"


class ProviderProofError(RuntimeError):
    """Value-free broker proof refusal safe for logs and CLI diagnostics."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _source_evidence(config_path: str | Path) -> dict[str, object]:
    """Read authoritative spec bytes and inode identity through one descriptor."""
    try:
        source = Path(config_path).expanduser().resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source, flags)
    except OSError as exc:
        raise ProviderProofError("provider_proof_source_unavailable") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProviderProofError("provider_proof_source_not_regular")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise ProviderProofError("provider_proof_source_unavailable") from exc
    finally:
        os.close(fd)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity:
        raise ProviderProofError("provider_proof_source_changed")
    content = b"".join(chunks)
    if len(content) != after.st_size:
        raise ProviderProofError("provider_proof_source_changed")
    return {
        "path": str(source),
        "device": after.st_dev,
        "inode": after.st_ino,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "bytes_sha256": hashlib.sha256(content).hexdigest(),
    }


def _effective_protocol(config: AgentConfig, base_url: str) -> str:
    """Name the protocol selected by the current runtime adapters."""
    path = urlsplit(base_url).path.rstrip("/")
    if path.endswith("/responses"):
        return "openai-responses"
    harness = str(getattr(config, "harness", "") or "").strip().lower()
    if harness in {"hermes", "openai", "openai-agents", "codex"}:
        return "openai-chat-completions" if harness == "hermes" else "openai-responses"
    return "anthropic-messages"


def _provider_evidence(config: AgentConfig) -> dict[str, object]:
    """Canonical provider-relevant effective config, never credential values."""
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None)
    base_url = str(getattr(provider, "base_url", "") or "").strip()
    raw_headers = getattr(provider, "extra_headers", {}) or {}
    headers: list[tuple[str, str]] = []
    if isinstance(raw_headers, Mapping):
        headers = sorted(
            ((str(name).casefold(), str(value)) for name, value in raw_headers.items()),
            key=lambda item: item[0],
        )
    return {
        "engine_key": str(getattr(config, "engine_key", "") or "").strip(),
        "model": str(
            getattr(claude, "model", "") or getattr(config, "model", "") or ""
        ).strip(),
        "base_url": base_url,
        "auth_env": str(getattr(provider, "auth_token_env", "") or "").strip(),
        "headers": headers,
        "protocol": _effective_protocol(config, base_url)
        if provider is not None
        else "",
    }


def provider_source_evidence(config_path: str | Path) -> dict[str, object]:
    """Capture the value-free source identity used around listener config load."""
    return _source_evidence(config_path)


def provider_proof(
    config: AgentConfig,
    config_path: str | Path,
    *,
    expected_source: Mapping[str, object] | None = None,
) -> str:
    """Return a versioned digest binding source identity/bytes and provider config."""
    source = _source_evidence(config_path)
    if expected_source is not None and dict(expected_source) != source:
        raise ProviderProofError("provider_proof_source_changed")
    payload = {
        "provider": _provider_evidence(config),
        "source": source,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _PROOF_PREFIX + hashlib.sha256(canonical).hexdigest()


def brokered_provider_proof_env(
    config: AgentConfig,
    config_path: str | Path,
    *,
    expected_source: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Environment overlay the listener passes to its lifecycle child."""
    return {
        BROKERED_LIFECYCLE_ENV: "1",
        EXPECTED_PROVIDER_PROOF_ENV: provider_proof(
            config, config_path, expected_source=expected_source
        ),
    }


def verify_brokered_provider_proof(
    config: AgentConfig,
    config_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail before lifecycle action if a brokered child's reload is not exact."""
    env = os.environ if environ is None else environ
    if BROKERED_LIFECYCLE_ENV not in env:
        return
    expected = str(env.get(EXPECTED_PROVIDER_PROOF_ENV) or "").strip()
    if not expected:
        raise ProviderProofError("provider_proof_missing")
    if (
        not expected.startswith(_PROOF_PREFIX)
        or len(expected) != len(_PROOF_PREFIX) + 64
    ):
        raise ProviderProofError("provider_proof_invalid")
    observed = provider_proof(config, config_path)
    if not hmac.compare_digest(expected, observed):
        raise ProviderProofError("provider_proof_mismatch")


def verify_brokered_provider_proof_for_agent(name: str) -> None:
    """Resolve/load one brokered restart target and verify it before action."""
    if BROKERED_LIFECYCLE_ENV not in os.environ:
        return
    try:
        from ..config import load_config
        from ..config._resolve import resolve_with_prefix

        config_path = resolve_with_prefix(name)
        config = load_config(config_path)
    except Exception as exc:
        raise ProviderProofError("provider_proof_child_load_failed") from exc
    verify_brokered_provider_proof(config, config_path)


def load_and_verify_brokered_provider_proof(
    config_path: str | Path,
    loader: Callable[[str | Path], AgentConfig],
) -> AgentConfig:
    """Load for a host start, redacting brokered load faults, then verify."""
    try:
        config = loader(config_path)
    except Exception as exc:
        if BROKERED_LIFECYCLE_ENV in os.environ:
            raise ProviderProofError("provider_proof_child_load_failed") from exc
        raise
    verify_brokered_provider_proof(config, config_path)
    return config


__all__ = [
    "BROKERED_LIFECYCLE_ENV",
    "EXPECTED_PROVIDER_PROOF_ENV",
    "ProviderProofError",
    "brokered_provider_proof_env",
    "load_and_verify_brokered_provider_proof",
    "provider_proof",
    "provider_source_evidence",
    "verify_brokered_provider_proof",
    "verify_brokered_provider_proof_for_agent",
]
