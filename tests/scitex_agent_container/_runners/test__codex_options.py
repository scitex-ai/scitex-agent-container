from __future__ import annotations

import os

from scitex_agent_container._runners import _codex_options


class _CodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _CodexModule:
    CodexConfig = _CodexConfig


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
