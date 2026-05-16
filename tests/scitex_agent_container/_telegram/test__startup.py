"""Tests for the sac listen / MCP startup hook (``maybe_start_bridge``)."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from scitex_agent_container._telegram import _runtime
from scitex_agent_container._telegram._startup import (
    LEAD_AUTH_TOKEN_ENV,
    maybe_start_bridge,
    mint_bridge_auth_token,
)


@dataclass
class _SpecStub:
    bot_token_env: str = "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
    allowed_users: list[str] | None = None
    auto_connect: bool = True


@contextlib.contextmanager
def _env(name: str, value: str | None) -> Iterator[None]:
    sentinel = object()
    prev: Any = os.environ.get(name, sentinel)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if prev is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def clean_runtime() -> Iterator[None]:
    _runtime.clear_bridge()
    yield
    _runtime.clear_bridge()


def test_returns_none_when_auto_connect_false() -> None:
    # Arrange
    spec = _SpecStub(auto_connect=False)

    # Act
    bridge = maybe_start_bridge(spec)

    # Assert
    assert bridge is None


def test_returns_none_when_lead_auth_token_missing() -> None:
    # Arrange
    spec = _SpecStub()

    # Act
    with _env(LEAD_AUTH_TOKEN_ENV, None):
        bridge = maybe_start_bridge(spec)

    # Assert
    assert bridge is None


def test_returns_none_when_bot_token_missing() -> None:
    # Arrange
    spec = _SpecStub(bot_token_env="SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN")

    # Act
    with (
        _env(LEAD_AUTH_TOKEN_ENV, "tok"),
        _env("SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN", None),
    ):
        bridge = maybe_start_bridge(spec)

    # Assert
    assert bridge is None


def test_returns_bridge_when_all_env_present() -> None:
    # Arrange
    spec = _SpecStub(allowed_users=["1"])

    # Act
    with (
        _env(LEAD_AUTH_TOKEN_ENV, "tok"),
        _env("SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN", "abc"),
    ):
        bridge = maybe_start_bridge(spec)

    # Assert
    assert bridge is not None


def test_returned_bridge_carries_allowed_users() -> None:
    # Arrange
    spec = _SpecStub(allowed_users=["1", "2"])

    # Act
    with (
        _env(LEAD_AUTH_TOKEN_ENV, "tok"),
        _env("SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN", "abc"),
    ):
        bridge = maybe_start_bridge(spec)

    # Assert
    assert bridge is not None and bridge.allowed_users == ["1", "2"]


def test_bridge_is_registered_in_runtime_singleton() -> None:
    # Arrange
    spec = _SpecStub(allowed_users=["1"])

    # Act
    with (
        _env(LEAD_AUTH_TOKEN_ENV, "tok-123"),
        _env("SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN", "abc"),
    ):
        bridge = maybe_start_bridge(spec)
        from_runtime = _runtime.get_bridge()

    # Assert
    assert from_runtime is bridge


def test_auth_token_is_recorded_from_env() -> None:
    # Arrange
    spec = _SpecStub(allowed_users=["1"])

    # Act
    with (
        _env(LEAD_AUTH_TOKEN_ENV, "AUTH-XYZ"),
        _env("SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN", "abc"),
    ):
        maybe_start_bridge(spec)
        token = _runtime.get_auth_token()

    # Assert
    assert token == "AUTH-XYZ"


def test_mint_bridge_auth_token_returns_unique_string() -> None:
    # Arrange
    first = mint_bridge_auth_token()

    # Act
    second = mint_bridge_auth_token()

    # Assert
    assert first != second
