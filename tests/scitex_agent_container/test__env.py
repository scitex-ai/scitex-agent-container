"""Tests for the SAC_ / SCITEX_AGENT_CONTAINER_ env-var helper."""

from __future__ import annotations

import pytest

from scitex_agent_container._env import (
    SacEnvConflict,
    aliases,
    getenv,
    setenv,
)


class TestGetenv:
    def test_short_form_only(self, monkeypatch):
        monkeypatch.setenv("SAC_HUB_URL", "https://hub")
        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HUB_URL", raising=False)
        assert getenv("HUB_URL") == "https://hub"

    def test_long_form_only(self, monkeypatch):
        monkeypatch.delenv("SAC_HUB_URL", raising=False)
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HUB_URL", "https://hub")
        assert getenv("HUB_URL") == "https://hub"

    def test_both_agree(self, monkeypatch):
        monkeypatch.setenv("SAC_HUB_URL", "https://hub")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HUB_URL", "https://hub")
        assert getenv("HUB_URL") == "https://hub"

    def test_both_disagree_raises(self, monkeypatch):
        monkeypatch.setenv("SAC_HUB_URL", "https://a")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HUB_URL", "https://b")
        with pytest.raises(SacEnvConflict, match="conflicts"):
            getenv("HUB_URL")

    def test_neither_set_returns_default(self, monkeypatch):
        monkeypatch.delenv("SAC_HUB_URL", raising=False)
        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HUB_URL", raising=False)
        assert getenv("HUB_URL", "fallback") == "fallback"

    def test_neither_set_returns_none_default(self, monkeypatch):
        monkeypatch.delenv("SAC_HUB_URL", raising=False)
        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HUB_URL", raising=False)
        assert getenv("HUB_URL") is None

    def test_both_empty_string_no_conflict(self, monkeypatch):
        monkeypatch.setenv("SAC_HUB_URL", "")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HUB_URL", "")
        assert getenv("HUB_URL") == ""

    def test_one_empty_one_set_raises(self, monkeypatch):
        # Inconsistent; user almost certainly meant to clear one.
        monkeypatch.setenv("SAC_HUB_URL", "")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HUB_URL", "https://hub")
        with pytest.raises(SacEnvConflict):
            getenv("HUB_URL")


class TestSetenv:
    def test_setenv_writes_both_forms(self, monkeypatch):
        monkeypatch.delenv("SAC_HUB_URL", raising=False)
        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HUB_URL", raising=False)
        setenv("HUB_URL", "https://hub")
        assert getenv("HUB_URL") == "https://hub"
        # Both forms readable individually
        import os

        assert os.environ["SAC_HUB_URL"] == "https://hub"
        assert os.environ["SCITEX_AGENT_CONTAINER_HUB_URL"] == "https://hub"


class TestAliases:
    def test_aliases_returns_both(self):
        short, long_ = aliases("HUB_URL")
        assert short == "SAC_HUB_URL"
        assert long_ == "SCITEX_AGENT_CONTAINER_HUB_URL"
