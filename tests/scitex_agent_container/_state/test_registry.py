"""Tests for the agent registry.

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), and asserts exactly one
fact (TQ007). Shared invariants (e.g. fields stored on ``add``) are
collapsed into ``pytest.parametrize`` so the matrix stays declarative.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.registry import Registry


def _make_registry(registry_dir):
    """Build a Registry pointed at ``registry_dir``.

    CI-only quirk: ``Registry(registry_dir=...)`` raises
    ``object.__new__() takes exactly one argument`` on hosted runners
    (Python 3.10/3.11/3.12 stable + pytest 8.4.2) but works on local
    Python 3.11.0rc1 with the same packages. Production calls
    ``Registry()`` argless everywhere, so the bug is test-path only.
    Bypass ``type.__call__`` by splitting __new__ and __init__ — the
    function bodies behave identically once the instance exists.
    """
    inst = object.__new__(Registry)
    Registry.__init__(inst, registry_dir=registry_dir)
    return inst


@pytest.fixture
def registry(tmp_path):
    """Create a registry with a temporary directory."""
    return _make_registry(tmp_path / "registry")


# ---------------------------------------------------------------------------
# add / get
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_with_one_agent(registry):
    """Registry seeded with one fully-specified entry."""
    registry.add(
        "test-agent",
        "/path/to/config.yaml",
        "cld-test",
        pid=12_345,
    )
    return registry


@pytest.mark.parametrize(
    "field,expected",
    [
        ("name", "test-agent"),
        ("config", "/path/to/config.yaml"),
        ("screen", "cld-test"),
        ("pid", 12_345),
    ],
)
def test_add_then_get_returns_entry_with_field_value(
    registry_with_one_agent, field, expected
):
    # Arrange
    registry = registry_with_one_agent
    # Act
    entry = registry.get("test-agent")
    # Assert
    assert entry[field] == expected


def test_add_then_get_records_started_at_timestamp_field(
    registry_with_one_agent,
):
    # Arrange
    registry = registry_with_one_agent
    # Act
    entry = registry.get("test-agent")
    # Assert
    assert "started_at" in entry


def test_get_returns_none_for_unregistered_agent_name(registry):
    # Arrange — fresh registry, no entries added.
    name = "nonexistent"
    # Act
    result = registry.get(name)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


def test_exists_returns_false_before_agent_is_added(registry):
    # Arrange — registry has no entries.
    name = "test-agent"
    # Act
    present = registry.exists(name)
    # Assert
    assert present is False


def test_exists_returns_true_after_agent_is_added(registry):
    # Arrange
    registry.add("test-agent", "/path/config.yaml", "cld-test")
    # Act
    present = registry.exists("test-agent")
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_deletes_existing_agent_entry(registry):
    # Arrange
    registry.add("test-agent", "/path/config.yaml", "cld-test")
    # Act
    registry.remove("test-agent")
    # Assert
    assert not registry.exists("test-agent")


def test_remove_is_silent_no_op_for_unknown_agent_name(registry):
    # Arrange — registry has no "nonexistent" entry.
    name = "nonexistent"
    # Act
    registry.remove(name)  # must not raise
    # Assert
    assert not registry.exists(name)


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_list_all_returns_empty_list_for_fresh_registry(registry):
    # Arrange — no entries.
    # Act
    entries = registry.list_all()
    # Assert
    assert entries == []


def test_list_all_returns_entry_count_matching_added_agents(registry):
    # Arrange
    registry.add("agent-a", "/a.yaml", "cld-a")
    registry.add("agent-b", "/b.yaml", "cld-b")
    # Act
    entries = registry.list_all()
    # Assert
    assert len(entries) == 2


def test_list_all_returns_entries_with_all_added_agent_names(registry):
    # Arrange
    registry.add("agent-a", "/a.yaml", "cld-a")
    registry.add("agent-b", "/b.yaml", "cld-b")
    # Act
    names = {e["name"] for e in registry.list_all()}
    # Assert
    assert names == {"agent-a", "agent-b"}


# ---------------------------------------------------------------------------
# add semantics: overwrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,expected",
    [
        ("config", "/new.yaml"),
        ("screen", "cld-new"),
    ],
)
def test_add_overwrites_existing_entry_field_with_latest_value(
    registry, field, expected
):
    # Arrange
    registry.add("test-agent", "/old.yaml", "cld-old")
    registry.add("test-agent", "/new.yaml", "cld-new")
    # Act
    entry = registry.get("test-agent")
    # Assert
    assert entry[field] == expected


# ---------------------------------------------------------------------------
# directory bootstrap
# ---------------------------------------------------------------------------


def test_registry_creates_missing_parent_directories_on_first_use(tmp_path):
    # Arrange
    reg_dir = tmp_path / "deep" / "nested" / "registry"
    # Act
    _make_registry(reg_dir)
    # Assert
    assert reg_dir.exists()


def test_registry_dir_creation_allows_subsequent_agent_add_to_persist(
    tmp_path,
):
    # Arrange
    reg_dir = tmp_path / "deep" / "nested" / "registry"
    registry = _make_registry(reg_dir)
    # Act
    registry.add("test", "/config.yaml", "cld-test")
    # Assert
    assert registry.exists("test")


# ---------------------------------------------------------------------------
# cleanup_stale: multiplexer probes
#
# Regression guard for the MBA false-alarm incident (2026-04-15): agents
# ran under tmux but cleanup_stale probed screen -ls only, causing the
# entire registry to be wiped and ``scitex-agent-container list`` to
# return [].
#
# PA-306: hand-rolled fake injection — save/restore the module attribute
# directly instead of using ``monkeypatch.setattr``.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_subprocess_run():
    """Yield a setter that swaps ``subprocess.run`` and restores it after."""
    import subprocess

    saved = subprocess.run

    def _install(fake):
        subprocess.run = fake  # type: ignore[assignment]

    try:
        yield _install
    finally:
        subprocess.run = saved  # type: ignore[assignment]


def _make_fake_run(tmux_alive: bool, screen_alive: bool):
    """Build a ``subprocess.run`` stub whose return codes mimic live sessions."""
    import subprocess

    def fake_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(cmd, returncode=1)
        result.stdout = ""
        if cmd[0] == "tmux":
            result.returncode = 0 if tmux_alive else 1
        else:  # screen
            result.returncode = 0 if screen_alive else 1
            result.stdout = cmd[-1] if screen_alive else ""
        return result

    return fake_run


def test_cleanup_stale_returns_zero_when_tmux_session_is_alive(
    registry, patched_subprocess_run
):
    # Arrange
    registry.add("alive-tmux-agent", "/config.yaml", "cld-alive")
    patched_subprocess_run(_make_fake_run(tmux_alive=True, screen_alive=False))
    # Act
    cleaned = registry.cleanup_stale()
    # Assert
    assert cleaned == 0


def test_cleanup_stale_keeps_entry_when_tmux_session_is_alive(
    registry, patched_subprocess_run
):
    # Arrange
    registry.add("alive-tmux-agent", "/config.yaml", "cld-alive")
    patched_subprocess_run(_make_fake_run(tmux_alive=True, screen_alive=False))
    # Act
    registry.cleanup_stale()
    # Assert
    assert registry.exists("alive-tmux-agent")


def test_cleanup_stale_returns_count_one_when_both_multiplexers_dead(
    registry, patched_subprocess_run
):
    # Arrange
    registry.add("dead-agent", "/config.yaml", "cld-dead")
    patched_subprocess_run(_make_fake_run(tmux_alive=False, screen_alive=False))
    # Act
    cleaned = registry.cleanup_stale()
    # Assert
    assert cleaned == 1


def test_cleanup_stale_removes_entry_when_both_multiplexers_dead(
    registry, patched_subprocess_run
):
    # Arrange
    registry.add("dead-agent", "/config.yaml", "cld-dead")
    patched_subprocess_run(_make_fake_run(tmux_alive=False, screen_alive=False))
    # Act
    registry.cleanup_stale()
    # Assert
    assert not registry.exists("dead-agent")
