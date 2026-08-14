"""The API-surface selection must match what the endpoint can actually serve.

No mocks anywhere. The environment is the real `os.environ`, saved and
restored by a yield fixture; the SDK stand-in is a real object carrying the
same `set_default_openai_api` attribute the module reads, so these tests
exercise the production lookup rather than a patched import.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

from scitex_agent_container._runners._openai_api_surface import (
    API_ENV,
    select_api_surface,
)

_KEYS = (API_ENV, "OPENAI_BASE_URL")

#: A self-hosted OpenAI-compatible gateway — the shape that 404s on
#: /v1/responses. This is the fleet's real litellm tunnel address.
_SELF_HOSTED = "http://127.0.0.1:18770/v1"


class _RecordingSDK:
    """Stands in for the `agents` module: same attribute, records the call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def set_default_openai_api(self, api: str) -> None:
        self.calls.append(api)


class _SDKWithoutTheKnob:
    """An SDK version that never grew `set_default_openai_api`."""


class _Env:
    """Writes real environment variables; the fixture undoes them."""

    def set(self, key: str, value: str) -> None:
        os.environ[key] = value


@pytest.fixture
def env() -> Iterator[_Env]:
    """Real env, cleared for the test and restored exactly afterwards.

    This is the yield-based save/restore the no-mocks doctrine prescribes
    in place of `monkeypatch.setenv`: production reads `os.environ`, so the
    test writes `os.environ`.
    """
    saved = {key: os.environ.get(key) for key in _KEYS}
    for key in _KEYS:
        os.environ.pop(key, None)
    try:
        yield _Env()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_a_self_hosted_base_url_selects_chat_completions(env: _Env) -> None:
    """The case that was broken: litellm serves only chat-completions, and
    the SDK's Responses default 404s against it."""
    # Arrange
    env.set("OPENAI_BASE_URL", _SELF_HOSTED)
    sdk = _RecordingSDK()

    # Act
    choice = select_api_surface(sdk)

    # Assert
    assert choice == "chat_completions"


def test_a_self_hosted_base_url_actually_calls_the_sdk_setter(
    env: _Env,
) -> None:
    """Returning the choice is not enough — the SDK must be reconfigured,
    which is the part that stops the 404."""
    # Arrange
    env.set("OPENAI_BASE_URL", _SELF_HOSTED)
    sdk = _RecordingSDK()

    # Act
    select_api_surface(sdk)

    # Assert
    assert sdk.calls == ["chat_completions"]


def test_no_base_url_leaves_the_sdk_default_untouched(env: _Env) -> None:
    """Talking to OpenAI proper should keep the richer surface, so with
    nothing configured the setter must not be called at all."""
    # Arrange
    sdk = _RecordingSDK()

    # Act
    select_api_surface(sdk)

    # Assert
    assert sdk.calls == []


def test_openais_own_host_keeps_responses(env: _Env) -> None:
    """A base URL is not itself evidence of a limited gateway — pointing at
    OpenAI explicitly must still get Responses."""
    # Arrange
    env.set("OPENAI_BASE_URL", "https://api.openai.com/v1")
    sdk = _RecordingSDK()

    # Act
    select_api_surface(sdk)

    # Assert
    assert sdk.calls == []


def test_the_env_override_wins_over_the_inference(env: _Env) -> None:
    """A gateway that DOES implement Responses must be able to say so, even
    though its URL is not OpenAI's."""
    # Arrange
    env.set("OPENAI_BASE_URL", "http://gateway.internal/v1")
    env.set(API_ENV, "responses")
    sdk = _RecordingSDK()

    # Act
    select_api_surface(sdk)

    # Assert
    assert sdk.calls == ["responses"]


def test_a_typo_in_the_override_falls_back_to_the_inference(
    env: _Env,
) -> None:
    """A misspelt override must not silently disable the fix — that would
    reintroduce the 404 while looking configured."""
    # Arrange
    env.set("OPENAI_BASE_URL", _SELF_HOSTED)
    env.set(API_ENV, "chat-completions")  # hyphen, not underscore

    sdk = _RecordingSDK()

    # Act
    select_api_surface(sdk)

    # Assert
    assert sdk.calls == ["chat_completions"]


def test_an_sdk_without_the_knob_is_not_an_error(env: _Env) -> None:
    """Version skew must degrade to the SDK's own default, not crash the
    turn — the caller has real work to do either way."""
    # Arrange
    env.set("OPENAI_BASE_URL", _SELF_HOSTED)
    sdk = _SDKWithoutTheKnob()

    # Act
    choice = select_api_surface(sdk)

    # Assert
    assert choice is None
