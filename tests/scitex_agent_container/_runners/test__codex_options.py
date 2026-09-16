from __future__ import annotations

import os

import pytest

from scitex_agent_container._runners import _codex_options


class _CodexConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _CodexModule:
    CodexConfig = _CodexConfig


def test_build_codex_config_reads_typed_provider_overrides_from_env():
    name = _codex_options.SAC_CODEX_CONFIG_OVERRIDES_ENV
    previous = os.environ.get(name)
    os.environ[name] = '["model_provider=\\"sac\\"","model=\\"qwen38-27b\\""]'
    try:
        config = _codex_options.build_codex_config(_CodexModule)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    assert config.kwargs["config_overrides"] == (
        'model_provider="sac"',
        'model="qwen38-27b"',
    )


def test_build_codex_config_refuses_malformed_provider_overrides():
    name = _codex_options.SAC_CODEX_CONFIG_OVERRIDES_ENV
    previous = os.environ.get(name)
    os.environ[name] = '{"not": "a list"}'
    try:
        with pytest.raises(ValueError, match="JSON array of strings"):
            _codex_options.build_codex_config(_CodexModule)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
