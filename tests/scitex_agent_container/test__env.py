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
        os.environ["SAC_HUB_URL"] = "https://hub"
        assert getenv("HUB_URL") == "https://hub"

    def test_long_form_only_returns_its_value(self, clean_env):
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        assert getenv("HUB_URL") == "https://hub"

    def test_both_forms_agreeing_returns_shared_value(self, clean_env):
        os.environ["SAC_HUB_URL"] = "https://hub"
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        assert getenv("HUB_URL") == "https://hub"

    def test_both_forms_disagreeing_raises_conflict(self, clean_env):
        os.environ["SAC_HUB_URL"] = "https://a"
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://b"
        with pytest.raises(SacEnvConflict, match="conflicts"):
            getenv("HUB_URL")

    def test_neither_set_returns_provided_default(self, clean_env):
        assert getenv("HUB_URL", "fallback") == "fallback"

    def test_neither_set_returns_none_when_no_default(self, clean_env):
        assert getenv("HUB_URL") is None

    def test_both_empty_string_returns_empty_string(self, clean_env):
        os.environ["SAC_HUB_URL"] = ""
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = ""
        assert getenv("HUB_URL") == ""

    def test_one_empty_one_set_raises_conflict(self, clean_env):
        # Inconsistent; user almost certainly meant to clear one.
        os.environ["SAC_HUB_URL"] = ""
        os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] = "https://hub"
        with pytest.raises(SacEnvConflict):
            getenv("HUB_URL")


class TestSetenv:
    def test_setenv_writes_both_forms(self, clean_env):
        setenv("HUB_URL", "https://hub")
        assert getenv("HUB_URL") == "https://hub"
        # Both forms readable individually
        assert os.environ["SAC_HUB_URL"] == "https://hub"
        assert os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] == "https://hub"


class TestAliases:
    def test_aliases_returns_both(self):
        short, long_ = aliases("HUB_URL")
        assert short == "SAC_HUB_URL"
        assert long_ == "SCITEX_AGENT_CONTAINER_HUB_URL"
