"""Launch retires telegrammer poller pidfiles left by a previous container.

Measured 2026-09-05 (scitex-compute-04, scitex-cards): the per-token pidfile in
the overlay home survives a restart, the pid namespace does not, and the new
poller SIGTERMed whatever had been handed the stale pid - the MCP server. Real
directories, no mocks. One assertion per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._stale_poller_pidfiles import (
    SERVER_LOCKFILE_NAME,
    TELEGRAMMER_RUNTIME_REL,
    clear_stale_poller_pidfiles,
)


def _runtime(home: Path, agent: str) -> Path:
    d = home / TELEGRAMMER_RUNTIME_REL / agent
    d.mkdir(parents=True)
    return d


def test_a_previous_incarnations_pidfile_is_removed(tmp_path: Path):
    # Arrange -- the shape the telegrammer writes: "<pid>\n<startMs>\n".
    pidfile = _runtime(tmp_path, "scitex-cards") / "poller-ece7d6cc.pid"
    pidfile.write_text("179\n1788598000000\n")
    # Act
    clear_stale_poller_pidfiles(tmp_path)
    # Assert
    assert not pidfile.exists()


def test_the_removed_files_are_reported_by_path(tmp_path: Path):
    # Arrange
    pidfile = _runtime(tmp_path, "scitex-cards") / "poller-ece7d6cc.pid"
    pidfile.write_text("179\n1\n")
    # Act
    removed = clear_stale_poller_pidfiles(tmp_path)
    # Assert
    assert removed == [pidfile]


def test_the_pollers_log_and_database_are_left_alone(tmp_path: Path):
    # Arrange -- the same dir holds the message store and the poller log.
    runtime = _runtime(tmp_path, "scitex-cards")
    (runtime / "poller-ece7d6cc.pid").write_text("179\n1\n")
    survivors = [
        runtime / "poller-ece7d6cc.log",
        runtime / "claude-code-telegrammer.db",
    ]
    for path in survivors:
        path.write_text("keep")
    # Act
    clear_stale_poller_pidfiles(tmp_path)
    # Assert
    assert all(path.exists() for path in survivors)


def test_a_home_with_no_telegrammer_state_is_a_no_op(tmp_path: Path):
    # Arrange -- nothing under the home at all.
    # Act
    removed = clear_stale_poller_pidfiles(tmp_path)
    # Assert
    assert removed == []


def test_the_mcp_servers_single_instance_lock_is_removed_too(tmp_path: Path):
    # Arrange -- lib/lock.ts writes "<pid>" and SIGTERMs it on the next start.
    lock = _runtime(tmp_path, "scitex-cards") / SERVER_LOCKFILE_NAME
    lock.write_text("171")
    # Act
    clear_stale_poller_pidfiles(tmp_path)
    # Assert
    assert not lock.exists()
