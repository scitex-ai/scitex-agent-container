"""Neutral external-gateway registry resolution."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from scitex_agent_container.config._external_gateway import (
    DEFAULT_EXTERNAL_GATEWAY_TOKEN_ENV,
    DEFAULT_EXTERNAL_GATEWAY_URL,
    EXTERNAL_GATEWAY_TOKEN_ENV_ENV,
    EXTERNAL_GATEWAY_URL_ENV,
    external_gateway_provider_entry,
)


@pytest.fixture
def clean_env() -> Iterator[dict[str, str]]:
    names = (EXTERNAL_GATEWAY_URL_ENV, EXTERNAL_GATEWAY_TOKEN_ENV_ENV)
    saved = {name: os.environ.pop(name) for name in names if name in os.environ}
    yield saved
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(saved)


def test_defaults_name_cross_host_gateway_and_neutral_token(clean_env):
    # Arrange
    # Act
    entry = external_gateway_provider_entry()
    # Assert
    assert entry == {
        "base_url": DEFAULT_EXTERNAL_GATEWAY_URL,
        "auth_token_env": DEFAULT_EXTERNAL_GATEWAY_TOKEN_ENV,
    }


def test_host_overrides_are_resolved_at_call_time(clean_env):
    # Arrange
    os.environ[EXTERNAL_GATEWAY_URL_ENV] = "http://egress.example:9000"
    os.environ[EXTERNAL_GATEWAY_TOKEN_ENV_ENV] = "SITE_GATEWAY_TOKEN"
    # Act
    entry = external_gateway_provider_entry()
    # Assert
    assert entry == {
        "base_url": "http://egress.example:9000",
        "auth_token_env": "SITE_GATEWAY_TOKEN",
    }
