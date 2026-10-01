from __future__ import annotations

import base64
import json
import os

import pytest

from scitex_agent_container._runners import _codex_options


class _CodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _CodexModule:
    CodexConfig = _CodexConfig


def test_build_codex_config_decodes_apptainer_safe_transport(env_save_restore):
    # Arrange
    overrides = ['model_provider="openai"', 'model_reasoning_effort="xhigh"']
    encoded = base64.b64encode(json.dumps(overrides).encode()).decode("ascii")
    env_save_restore.set(_codex_options.SAC_CODEX_CONFIG_OVERRIDES_B64_ENV, encoded)
    # Act
    config = _codex_options.build_codex_config(_CodexModule)
    # Assert
    assert config.kwargs["config_overrides"] == tuple(overrides)


@pytest.mark.parametrize("encoded", ["%%%", "/w=="])
def test_build_codex_config_refuses_invalid_transport(encoded, env_save_restore):
    # Arrange
    env_save_restore.set(_codex_options.SAC_CODEX_CONFIG_OVERRIDES_B64_ENV, encoded)
    # Act / Assert
    with pytest.raises(ValueError, match="must encode UTF-8 JSON"):
        _codex_options.build_codex_config(_CodexModule)


def test_build_codex_config_reads_typed_provider_overrides_from_env():
    # Arrange
    name = _codex_options.SAC_CODEX_CONFIG_OVERRIDES_ENV
    previous = os.environ.get(name)
    os.environ[name] = '["model_provider=\\"sac\\"","model=\\"qwen38-27b\\""]'
    try:
        # Act
        config = _codex_options.build_codex_config(_CodexModule)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    # Assert
    assert config.kwargs["config_overrides"] == (
        'model_provider="sac"',
        'model="qwen38-27b"',
    )


def test_build_codex_config_refuses_malformed_provider_overrides():
    # Arrange
    name = _codex_options.SAC_CODEX_CONFIG_OVERRIDES_ENV
    previous = os.environ.get(name)
    os.environ[name] = '{"not": "a list"}'
    try:
        # Act
        try:
            _codex_options.build_codex_config(_CodexModule)
            error = None
        except ValueError as exc:
            error = exc
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    # Assert
    assert error is not None and "JSON array of strings" in str(error)
