"""The fleet Qwen gateway address — one home, resolved not frozen.

The migration writes ``provider: qwen-gateway`` into every spec instead of an
address, so these pin the two properties that makes that safe: the name is
REGISTERED (an unregistered one refuses at start), and the address is resolved
through :func:`resolve_provider` at call time so a per-host override works.

No mocks. The environment is a real seam, saved and restored around each test
so no other test — or the operator's shell — is disturbed.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.config._provider_registry import (
    list_providers,
    resolve_provider,
)
from scitex_agent_container.config._qwen_gateway import (
    DEFAULT_QWEN_GATEWAY_TOKEN_ENV,
    DEFAULT_QWEN_GATEWAY_URL,
    QWEN_GATEWAY_HOST,
    QWEN_GATEWAY_PROVIDER,
    QWEN_GATEWAY_TOKEN_ENV_ENV,
    QWEN_GATEWAY_URL_ENV,
    qwen_gateway_token_env,
    qwen_gateway_url,
)


@pytest.fixture
def clean_env():
    """Both override variables removed, and restored afterwards."""
    keys = (QWEN_GATEWAY_URL_ENV, QWEN_GATEWAY_TOKEN_ENV_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    for key in keys:
        os.environ.pop(key, None)
    try:
        yield os.environ
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_gateway_is_a_registered_provider_name() -> None:
    # Arrange
    names = list_providers()
    # Act
    registered = QWEN_GATEWAY_PROVIDER in names
    # Assert
    assert registered is True


def test_the_default_address_uses_the_spelling_that_resolves(clean_env) -> None:
    # Arrange — compute-04 and compute-04-lan both answer 000; this one 401.
    expected = QWEN_GATEWAY_HOST
    # Act
    url = qwen_gateway_url()
    # Assert
    assert expected in url


def test_the_default_address_is_the_measured_one(clean_env) -> None:
    # Arrange
    expected = DEFAULT_QWEN_GATEWAY_URL
    # Act
    url = qwen_gateway_url()
    # Assert
    assert url == expected


def test_resolve_provider_returns_the_gateway_address(clean_env) -> None:
    # Arrange
    expected = DEFAULT_QWEN_GATEWAY_URL
    # Act
    entry = resolve_provider(QWEN_GATEWAY_PROVIDER)
    # Assert
    assert entry["base_url"] == expected


def test_resolve_provider_names_the_key_env_var_not_the_key(clean_env) -> None:
    # Arrange
    expected = DEFAULT_QWEN_GATEWAY_TOKEN_ENV
    # Act
    entry = resolve_provider(QWEN_GATEWAY_PROVIDER)
    # Assert
    assert entry["auth_token_env"] == expected


def test_a_host_override_wins_over_the_default(clean_env) -> None:
    # Arrange
    clean_env[QWEN_GATEWAY_URL_ENV] = "http://100.64.0.1:18772"
    # Act
    url = qwen_gateway_url()
    # Assert
    assert url == "http://100.64.0.1:18772"


def test_the_override_reaches_resolve_provider(clean_env) -> None:
    # Arrange — a frozen module constant would ignore an export made later.
    clean_env[QWEN_GATEWAY_URL_ENV] = "http://100.64.0.1:18772"
    # Act
    entry = resolve_provider(QWEN_GATEWAY_PROVIDER)
    # Assert
    assert entry["base_url"] == "http://100.64.0.1:18772"


def test_a_blank_override_is_treated_as_unset(clean_env) -> None:
    # Arrange — an exported-but-empty variable is how a shell says nothing.
    clean_env[QWEN_GATEWAY_URL_ENV] = "   "
    # Act
    url = qwen_gateway_url()
    # Assert
    assert url == DEFAULT_QWEN_GATEWAY_URL


def test_the_token_env_name_is_overridable_too(clean_env) -> None:
    # Arrange — handyman-08 names a different variable than its siblings.
    clean_env[QWEN_GATEWAY_TOKEN_ENV_ENV] = "SAC_LOCAL_GPTOSS_KEY"
    # Act
    name = qwen_gateway_token_env()
    # Assert
    assert name == "SAC_LOCAL_GPTOSS_KEY"


def test_an_unregistered_provider_still_resolves_to_nothing() -> None:
    # Arrange — the dynamic entry must not make every name resolvable.
    unknown = "no-such-gateway"
    # Act
    entry = resolve_provider(unknown)
    # Assert
    assert entry is None
