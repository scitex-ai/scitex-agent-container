"""Tests for the agent registry."""

from __future__ import annotations

import pytest

from scitex_agent_container.registry import Registry


@pytest.fixture
def registry(tmp_path):
    """Create a registry with a temporary directory."""
    # CI-diagnostic: surface what Registry is actually being resolved to.
    import sys

    print(
        f"[CI-DEBUG] Registry={Registry!r} module={Registry.__module__} "
        f"file={getattr(sys.modules.get(Registry.__module__), '__file__', '?')} "
        f"init={Registry.__init__!r} "
        f"new={Registry.__new__!r} "
        f"call={type(Registry).__call__!r} "
        f"meta={type(Registry)!r} "
        f"mro={Registry.__mro__!r}",
        file=sys.stderr,
        flush=True,
    )
    # Try positional first to isolate kwarg vs args issue.
    try:
        return Registry(tmp_path / "registry")
    except TypeError as e:
        print(f"[CI-DEBUG] positional also failed: {e}", file=sys.stderr, flush=True)
        # Fallback to original failure path so test still surfaces normally.
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

    def test_cleanup_stale_keeps_tmux_sessions(self, registry, monkeypatch):
        """cleanup_stale must NOT remove entries for live tmux sessions.

        Regression test for the MBA false-alarm incident (2026-04-15): agents
        ran under tmux but cleanup_stale probed screen -ls only, causing the
        entire registry to be wiped and scitex-agent-container list to return [].
        """
        import subprocess

        registry.add("alive-tmux-agent", "/config.yaml", "cld-alive")

        # Simulate: tmux reports session alive, screen reports nothing
        def fake_run(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, returncode=0)
            result.stdout = ""
            if cmd[0] == "tmux":
                result.returncode = 0  # tmux has-session succeeds
            else:
                result.returncode = 1  # screen -ls finds nothing
                result.stdout = ""
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        cleaned = registry.cleanup_stale()
        assert cleaned == 0, "Must not remove entry when tmux session is alive"
        assert registry.exists("alive-tmux-agent")

    def test_cleanup_stale_removes_dead_sessions(self, registry, monkeypatch):
        """cleanup_stale removes entries absent from both tmux and screen."""
        import subprocess

        registry.add("dead-agent", "/config.yaml", "cld-dead")

        def fake_run(cmd, **kwargs):
            result = subprocess.CompletedProcess(cmd, returncode=1)
            result.stdout = ""
            return result

        monkeypatch.setattr("subprocess.run", fake_run)

        cleaned = registry.cleanup_stale()
        assert cleaned == 1
        assert not registry.exists("dead-agent")
