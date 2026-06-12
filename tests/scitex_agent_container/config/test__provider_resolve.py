"""Tests for ``config._provider_resolve.resolve_provider_spec``.

Operator directive 2026-06-08: resolution projects the sealed
operator-facing union (RegistryProvider / CustomProvider) onto a
single typed ResolvedProvider surface. Pins the model precedence
chain (CustomProvider.model → RegistryProvider.model_override →
registry default_model → ClaudeSpec.model → loud error) and the
endpoint shape mapping (DirectEndpoint → base_url; TunneledEndpoint
→ tunnel field, base_url empty until ``with_tunneled_base_url``).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import (
    CustomProvider,
    DirectEndpoint,
    RegistryProvider,
    TunneledEndpoint,
    TunnelSpec,
)
from scitex_agent_container.config._provider_resolve import (
    ProviderResolutionError,
    resolve_provider_spec,
    with_tunneled_base_url,
)

_REGISTRY = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": {"base_url": "https://api.deepseek.com/anthropic"},
        "default_model": "deepseek-chat",
        "auth_token_env": "DEEPSEEK_API_KEY",
    },
    "qwen-spartan": {
        "label": "Qwen vLLM (Spartan)",
        "endpoint": {
            "tunnel": {
                "jump_host": "spartan-login",
                "target_host": "spartan-gpgpu171",
                "remote_port": 4000,
            }
        },
        "default_model": "qwen36-35b-a3b",
        "auth_token_env": "CLEW_VLLM_TOKEN",
    },
}


# ---------------------------------------------------------------------------
# RegistryProvider resolution
# ---------------------------------------------------------------------------


def test_registry_provider_uses_registry_endpoint_base_url():
    # Arrange
    spec = RegistryProvider(name="deepseek")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.base_url == "https://api.deepseek.com/anthropic"


def test_registry_provider_uses_registry_default_model_when_no_override():
    # Arrange
    spec = RegistryProvider(name="deepseek")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.model == "deepseek-chat"


def test_registry_provider_model_override_wins_over_registry_default():
    # Arrange
    spec = RegistryProvider(name="deepseek", model_override="deepseek-reasoner")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.model == "deepseek-reasoner"


def test_registry_provider_auth_env_comes_from_registry():
    # Arrange
    spec = RegistryProvider(name="deepseek")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.auth_token_env == "DEEPSEEK_API_KEY"


def test_registry_provider_label_comes_from_registry():
    # Arrange
    spec = RegistryProvider(name="deepseek")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.label == "DeepSeek"


def test_registry_provider_unknown_name_raises_resolution_error():
    # Arrange
    spec = RegistryProvider(name="ghost")
    # Act
    ctx = pytest.raises(ProviderResolutionError)
    # Assert
    with ctx:
        resolve_provider_spec(spec, _REGISTRY)


def test_registry_provider_tunneled_endpoint_populates_tunnel_field():
    # Arrange
    spec = RegistryProvider(name="qwen-spartan")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert isinstance(resolved.tunnel, TunnelSpec)
    assert resolved.tunnel.jump_host == "spartan-login"


def test_registry_provider_tunneled_endpoint_base_url_empty_at_resolve_time():
    # Arrange — the tunnel manager hasn't bound a port yet; the
    # caller overlays via with_tunneled_base_url after up().
    spec = RegistryProvider(name="qwen-spartan")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.base_url == ""


# ---------------------------------------------------------------------------
# CustomProvider resolution
# ---------------------------------------------------------------------------


def _custom_direct() -> CustomProvider:
    return CustomProvider(
        label="internal-gw",
        endpoint=DirectEndpoint(base_url="https://internal.example/anthropic"),
        model="qwen36-35b-a3b",
        auth_token_env="CLEW_VLLM_TOKEN",
    )


def test_custom_direct_endpoint_flows_base_url_through_unchanged():
    # Arrange
    spec = _custom_direct()
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.base_url == "https://internal.example/anthropic"


def test_custom_direct_resolved_model_matches_custom_model():
    # Arrange
    spec = _custom_direct()
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.model == "qwen36-35b-a3b"


def test_custom_direct_resolved_label_matches_custom_label():
    # Arrange
    spec = _custom_direct()
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.label == "internal-gw"


def test_custom_direct_resolved_carries_no_tunnel():
    # Arrange
    spec = _custom_direct()
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.tunnel is None


def test_custom_tunneled_endpoint_populates_tunnel_and_empty_base_url():
    # Arrange
    spec = CustomProvider(
        label="qwen",
        endpoint=TunneledEndpoint(
            tunnel=TunnelSpec(jump_host="j", target_host="t", remote_port=4000)
        ),
        model="qwen36-35b-a3b",
        auth_token_env="CLEW_VLLM_TOKEN",
    )
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Assert
    assert resolved.base_url == ""
    assert resolved.tunnel.target_host == "t"


# ---------------------------------------------------------------------------
# Model precedence chain — CustomProvider step 4 (ClaudeSpec fallback)
# ---------------------------------------------------------------------------


def test_custom_provider_with_empty_model_falls_back_to_claude_model():
    # Arrange — legacy back-compat shape lands here: CustomProvider
    # carries model="" so the resolver falls back to ClaudeSpec.model.
    spec = CustomProvider(
        label="legacy:test",
        endpoint=DirectEndpoint(base_url="https://x.example"),
        model="",
        auth_token_env="X_KEY",
    )
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY, claude_model_fallback="opus")
    # Assert
    assert resolved.model == "opus"


def test_custom_provider_no_model_anywhere_raises_resolution_error():
    # Arrange — empty model + no fallback.
    spec = CustomProvider(
        label="legacy:test",
        endpoint=DirectEndpoint(base_url="https://x.example"),
        model="",
        auth_token_env="X_KEY",
    )
    # Act
    ctx = pytest.raises(ProviderResolutionError)
    # Assert
    with ctx:
        resolve_provider_spec(spec, _REGISTRY, claude_model_fallback="")


# ---------------------------------------------------------------------------
# Model precedence chain — RegistryProvider full chain
# ---------------------------------------------------------------------------


def test_registry_override_wins_over_claude_fallback():
    # Arrange — override beats claude.model.
    spec = RegistryProvider(name="deepseek", model_override="custom-id")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY, claude_model_fallback="opus")
    # Assert
    assert resolved.model == "custom-id"


def test_registry_default_wins_over_claude_fallback():
    # Arrange
    spec = RegistryProvider(name="deepseek")
    # Act
    resolved = resolve_provider_spec(spec, _REGISTRY, claude_model_fallback="opus")
    # Assert
    assert resolved.model == "deepseek-chat"


def test_registry_provider_no_default_falls_back_to_claude_model():
    # Arrange — registry entry with default_model=None (operator
    # overlays or the "mimo" built-in).
    registry = dict(_REGISTRY)
    registry["mimo"] = {
        "label": "MiMo",
        "endpoint": {"base_url": "https://mimo.example"},
        "default_model": None,
        "auth_token_env": "XIAOMI_API_KEY",
    }
    spec = RegistryProvider(name="mimo")
    # Act
    resolved = resolve_provider_spec(
        spec, registry, claude_model_fallback="claude-3-5-sonnet-20241022"
    )
    # Assert
    assert resolved.model == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# with_tunneled_base_url — overlay live local port
# ---------------------------------------------------------------------------


def test_with_tunneled_base_url_sets_localhost_url_with_port():
    # Arrange
    spec = RegistryProvider(name="qwen-spartan")
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Act
    live = with_tunneled_base_url(resolved, local_port=14000)
    # Assert
    assert live.base_url == "http://localhost:14000"


def test_with_tunneled_base_url_preserves_label_and_auth_env():
    # Arrange
    spec = RegistryProvider(name="qwen-spartan")
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Act
    live = with_tunneled_base_url(resolved, local_port=14000)
    # Assert
    assert live.label == "Qwen vLLM (Spartan)"
    assert live.auth_token_env == "CLEW_VLLM_TOKEN"


def test_with_tunneled_base_url_on_non_tunneled_raises_resolution_error():
    # Arrange
    spec = _custom_direct()
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Act
    ctx = pytest.raises(ProviderResolutionError)
    # Assert
    with ctx:
        with_tunneled_base_url(resolved, local_port=14000)


def test_with_tunneled_base_url_zero_port_raises_resolution_error():
    # Arrange
    spec = RegistryProvider(name="qwen-spartan")
    resolved = resolve_provider_spec(spec, _REGISTRY)
    # Act
    ctx = pytest.raises(ProviderResolutionError)
    # Assert
    with ctx:
        with_tunneled_base_url(resolved, local_port=0)
