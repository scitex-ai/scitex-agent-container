"""Tests for the SAC_ / SCITEX_AGENT_CONTAINER_ env-var helper."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._env import (
    SacEnvConflict,
    aliases,
    getenv,
    setenv,
)

_KEYS = ("SAC_HUB_URL", "SCITEX_AGENT_CONTAINER_HUB_URL")


@pytest.fixture
def clean_env():
    """Snapshot the SAC env keys, clear them, restore on teardown.

    Pure env save/restore — no production-internals patching.
    """
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestGetenv:
    def test_short_form_only_returns_its_value(self, clean_env):
        # Arrange
        os.environ["SAC_HUB_URL"] = "https://hub"
        # Act
        value = getenv("HUB_URL")
        # Assert
        assert value == "https://hub"

    def test_long_form_only_returns_its_value(self, clean_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        # Act
        value = getenv("HUB_URL")
        # Assert
        assert value == "https://hub"

    def test_both_forms_agreeing_returns_shared_value(self, clean_env):
        # Arrange
        os.environ["SAC_HUB_URL"] = "https://hub"
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        # Act
        value = getenv("HUB_URL")
        # Assert
        assert value == "https://hub"

    def test_both_forms_disagreeing_raises_conflict(self, clean_env):
        # Arrange
        os.environ["SAC_HUB_URL"] = "https://a"
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://b"
        ctx = pytest.raises(SacEnvConflict, match="conflicts")
        # Act
        action = getenv
        # Assert
        with ctx:
            action("HUB_URL")

    def test_neither_set_returns_provided_default(self, clean_env):
        # Arrange: clean_env fixture cleared both keys.
        default = "fallback"
        # Act
        value = getenv("HUB_URL", default)
        # Assert
        assert value == "fallback"

    def test_neither_set_returns_none_when_no_default(self, clean_env):
        # Arrange: clean_env fixture cleared both keys.
        # Act
        value = getenv("HUB_URL")
        # Assert
        assert value is None

    def test_both_empty_string_returns_empty_string(self, clean_env):
        # Arrange
        os.environ["SAC_HUB_URL"] = ""
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = ""
        # Act
        value = getenv("HUB_URL")
        # Assert
        assert value == ""

    def test_one_empty_one_set_raises_conflict(self, clean_env):
        # Arrange: inconsistent — user almost certainly meant to clear one.
        os.environ["SAC_HUB_URL"] = ""
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        ctx = pytest.raises(SacEnvConflict)
        # Act
        action = getenv
        # Assert
        with ctx:
            action("HUB_URL")


class TestSetenv:
    def test_setenv_round_trip_via_getenv(self, clean_env):
        # Arrange
        key = "HUB_URL"
        # Act
        setenv(key, "https://hub")
        # Assert
        assert getenv(key) == "https://hub"

    def test_setenv_writes_short_form(self, clean_env):
        # Arrange
        key = "HUB_URL"
        # Act
        setenv(key, "https://hub")
        # Assert
        assert os.environ["SAC_HUB_URL"] == "https://hub"

    def test_setenv_writes_long_form(self, clean_env):
        # Arrange
        key = "HUB_URL"
        # Act
        setenv(key, "https://hub")
        # Assert
        assert os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] == "https://hub"


class TestAliases:
    def test_aliases_returns_short_form_first(self):
        # Arrange
        key = "HUB_URL"
        # Act
        short, _long = aliases(key)
        # Assert
        assert short == "SAC_HUB_URL"

    def test_aliases_returns_long_form_second(self):
        # Arrange
        key = "HUB_URL"
        # Act
        _short, long_ = aliases(key)
        # Assert
        assert long_ == "SCITEX_AGENT_CONTAINER_HUB_URL"
