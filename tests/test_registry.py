"""Tests for the agent registry."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scitex_agent_container.registry import Registry


@pytest.fixture
def registry(tmp_path):
    """Create a registry with a temporary directory."""
    return Registry(registry_dir=tmp_path / "registry")


class TestRegistry:
    def test_add_and_get(self, registry):
        registry.add("test-agent", "/path/to/config.yaml", "cld-test", pid=12345)
        entry = registry.get("test-agent")
        assert entry is not None
        assert entry["name"] == "test-agent"
        assert entry["config"] == "/path/to/config.yaml"
        assert entry["screen"] == "cld-test"
        assert entry["pid"] == 12345
        assert "started_at" in entry

    def test_get_nonexistent(self, registry):
        assert registry.get("nonexistent") is None

    def test_exists(self, registry):
        assert not registry.exists("test-agent")
        registry.add("test-agent", "/path/config.yaml", "cld-test")
        assert registry.exists("test-agent")

    def test_remove(self, registry):
        registry.add("test-agent", "/path/config.yaml", "cld-test")
        assert registry.exists("test-agent")
        registry.remove("test-agent")
        assert not registry.exists("test-agent")

    def test_remove_nonexistent(self, registry):
        # Should not raise
        registry.remove("nonexistent")

    def test_list_all_empty(self, registry):
        entries = registry.list_all()
        assert entries == []

    def test_list_all(self, registry):
        registry.add("agent-a", "/a.yaml", "cld-a")
        registry.add("agent-b", "/b.yaml", "cld-b")
        entries = registry.list_all()
        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert names == {"agent-a", "agent-b"}

    def test_add_overwrites(self, registry):
        registry.add("test-agent", "/old.yaml", "cld-old")
        registry.add("test-agent", "/new.yaml", "cld-new")
        entry = registry.get("test-agent")
        assert entry["config"] == "/new.yaml"
        assert entry["screen"] == "cld-new"

    def test_registry_dir_created(self, tmp_path):
        reg_dir = tmp_path / "deep" / "nested" / "registry"
        registry = Registry(registry_dir=reg_dir)
        registry.add("test", "/config.yaml", "cld-test")
        assert reg_dir.exists()
        assert registry.exists("test")
