"""Three-shape provider parser tests (Form A / B / C + legacy back-compat).

Operator directive 2026-06-08: ``_parse_provider`` produces a typed
sealed-union ProviderSpec (RegistryProvider XOR CustomProvider) for
each of the three operator-facing shapes, and normalises the legacy
``{base_url, auth_token_env}`` dict into a CustomProvider with a
``label="legacy:<agent-name>"`` and a one-time stderr deprecation
warning. Each test pins one observable fact (TQ007), AAA markers
(TQ002), descriptive name with >=3 tokens (TQ003).

The parser uses a real registry — tests pass a minimal dict via the
``registry`` kwarg so the parsed shapes are decoupled from whatever
``providers.d/*.yaml`` the test host might carry.
"""

from __future__ import annotations

from scitex_agent_container.config import (
    CustomProvider,
    DirectEndpoint,
    RegistryProvider,
    TunneledEndpoint,
)
from scitex_agent_container.config._parsers._claude import _parse_provider

_REGISTRY = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": {"base_url": "https://api.deepseek.com/anthropic"},
        "default_model": "deepseek-chat",
        "auth_token_env": "DEEPSEEK_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic (default)",
        "endpoint": None,
        "default_model": None,
        "auth_token_env": None,
    },
}


# ---------------------------------------------------------------------------
# Form A — bare string
# ---------------------------------------------------------------------------


def test_form_a_bare_string_yields_registry_provider():
    # Arrange
    spec = {"provider": "deepseek"}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert isinstance(result, RegistryProvider)


def test_form_a_bare_string_carries_name():
    # Arrange
    spec = {"provider": "deepseek"}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result.name == "deepseek"


def test_form_a_anthropic_sentinel_yields_none():
    # Arrange — endpoint=None sentinel means "no override".
    spec = {"provider": "anthropic"}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None


def test_form_a_unknown_string_yields_none_for_validator_to_catch():
    # Arrange — parser stays defensive; validator surfaces the loud error.
    spec = {"provider": "no-such-provider"}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Form B — registry name + optional model override
# ---------------------------------------------------------------------------


def test_form_b_name_only_yields_registry_provider_with_no_override():
    # Arrange
    spec = {"provider": {"name": "deepseek"}}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert isinstance(result, RegistryProvider)
    assert result.model_override is None


def test_form_b_with_model_override_populates_field():
    # Arrange
    spec = {"provider": {"name": "deepseek", "model": "deepseek-reasoner"}}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result.model_override == "deepseek-reasoner"


def test_form_b_anthropic_sentinel_collapses_to_none():
    # Arrange
    spec = {"provider": {"name": "anthropic"}}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Form C — type: custom
# ---------------------------------------------------------------------------


_CUSTOM_DIRECT = {
    "provider": {
        "type": "custom",
        "label": "internal-gw",
        "endpoint": {"base_url": "https://internal.example/anthropic"},
        "model": "qwen36-35b-a3b",
        "auth_token_env": "INTERNAL_KEY",
    }
}


def test_form_c_direct_endpoint_yields_custom_provider():
    # Arrange
    spec = dict(_CUSTOM_DIRECT)
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert isinstance(result, CustomProvider)


def test_form_c_direct_endpoint_carries_typed_direct_endpoint():
    # Arrange
    spec = dict(_CUSTOM_DIRECT)
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert isinstance(result.endpoint, DirectEndpoint)
    assert result.endpoint.base_url == "https://internal.example/anthropic"


def test_form_c_tunnel_endpoint_carries_typed_tunneled_endpoint():
    # Arrange
    spec = {
        "provider": {
            "type": "custom",
            "label": "qwen",
            "endpoint": {
                "tunnel": {
                    "jump_host": "spartan-login",
                    "target_host": "spartan-gpgpu171",
                    "remote_port": 4000,
                }
            },
            "model": "qwen36-35b-a3b",
            "auth_token_env": "CLEW_VLLM_TOKEN",
        }
    }
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert isinstance(result.endpoint, TunneledEndpoint)
    assert result.endpoint.tunnel.jump_host == "spartan-login"


def test_form_c_allowed_tools_list_populates_field():
    # Arrange
    block = dict(_CUSTOM_DIRECT["provider"], allowed_tools=["Bash", "Read"])
    # Act
    result = _parse_provider({"provider": block}, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result.allowed_tools == ["Bash", "Read"]


def test_form_c_missing_required_field_yields_none():
    # Arrange — parser defends by returning None; validator is the loud surface.
    block = {k: v for k, v in _CUSTOM_DIRECT["provider"].items() if k != "model"}
    # Act
    result = _parse_provider({"provider": block}, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Legacy back-compat — dict without name/type but with base_url
# ---------------------------------------------------------------------------


def test_legacy_dict_normalises_to_custom_provider():
    # Arrange
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    result = _parse_provider(spec, agent_name="ds", registry=_REGISTRY)
    # Assert
    assert isinstance(result, CustomProvider)


def test_legacy_dict_uses_legacy_label_with_agent_name():
    # Arrange
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    result = _parse_provider(spec, agent_name="ds", registry=_REGISTRY)
    # Assert
    assert result.label == "legacy:ds"


def test_legacy_dict_carries_direct_endpoint_with_base_url():
    # Arrange
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    result = _parse_provider(spec, agent_name="ds", registry=_REGISTRY)
    # Assert
    assert isinstance(result.endpoint, DirectEndpoint)
    assert result.endpoint.base_url == "https://api.deepseek.com/anthropic"


def test_legacy_dict_carries_empty_model_for_resolver_fallback():
    # Arrange — legacy shape has no model; resolver falls back to
    # ClaudeSpec.model via the precedence chain.
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    result = _parse_provider(spec, agent_name="ds", registry=_REGISTRY)
    # Assert
    assert result.model == ""


def test_legacy_dict_emits_one_time_deprecation_warning(capsys):
    # Arrange
    import scitex_agent_container.config._parsers._claude as parser_mod

    parser_mod._LEGACY_WARN_EMITTED.clear()
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    _parse_provider(spec, agent_name="agent-once", registry=_REGISTRY)
    captured = capsys.readouterr().err
    # Assert
    assert "deprecation" in captured
    assert "agent-once" in captured
    assert "legacy dict form" in captured


def test_legacy_dict_warning_fires_only_once_per_agent_name(capsys):
    # Arrange — two parses for the same agent name should emit ONE warning.
    import scitex_agent_container.config._parsers._claude as parser_mod

    parser_mod._LEGACY_WARN_EMITTED.clear()
    spec = {
        "provider": {
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_token_env": "DEEPSEEK_API_KEY",
        }
    }
    # Act
    _parse_provider(spec, agent_name="repeat", registry=_REGISTRY)
    _parse_provider(spec, agent_name="repeat", registry=_REGISTRY)
    captured = capsys.readouterr().err
    # Assert
    assert captured.count("deprecation") == 1


# ---------------------------------------------------------------------------
# Garbage shapes — defensive parser returns None
# ---------------------------------------------------------------------------


def test_garbage_non_string_non_dict_yields_none():
    # Arrange
    spec = {"provider": 42}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None


def test_unrecognised_dict_shape_yields_none():
    # Arrange — dict without name/type/base_url; validator surfaces the loud error.
    spec = {"provider": {"label": "x"}}
    # Act
    result = _parse_provider(spec, agent_name="t1", registry=_REGISTRY)
    # Assert
    assert result is None
