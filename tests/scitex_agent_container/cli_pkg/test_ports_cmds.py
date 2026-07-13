"""Tests for ``cli_pkg.ports_cmds`` — the ``sac ports`` inventory.

PA-306 no-mocks: every collaborator is real.

* ``CliRunner`` invokes the real Click command.
* A real ``state.db`` under ``tmp_path`` carries real a2a claims made
  through :mod:`_state.port_allocator` — the same allocator production
  uses.
* Liveness is exercised against REAL sockets: a bound-and-listening
  socket (live) and a bound-then-closed free port (dead / orphan). No
  probe is monkeypatched.
* A yield-based ``home_in_tmp`` fixture overrides ``$HOME`` via
  ``os.environ`` so the CLI smoke tests read/write an isolated
  ``~/.scitex`` under ``tmp_path`` (no monkeypatch).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state import port_allocator as pa
from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.ports_cmds import (
    _reference_map,
    collect_ports_data,
)

# ---------------------------------------------------------------------------
# Fixtures (real collaborators, no monkeypatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A per-test state.db path; the allocator creates schema on demand."""
    return tmp_path / "state.db"


@pytest.fixture
def listening_port():
    """A real listening TCP socket on loopback; yields its port.

    ``port_is_bound`` connects to it, so it must actually ``listen()``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def free_port() -> int:
    """A port that was bound then released — almost certainly nothing
    listens on it, so a probe reports it dead (orphan)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def home_in_tmp(tmp_path):
    """Point Path.home() / expanduser at tmp_path via HOME. No monkeypatch."""
    prior = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = str(tmp_path)
    os.environ["USERPROFILE"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# collect_ports_data — listen row
# ---------------------------------------------------------------------------


def test_collect_reports_listen_port_at_given_bind(db: Path) -> None:
    # Arrange
    data = collect_ports_data(db_path=db, listen_host="127.0.0.1", listen_port=7878)
    # Act
    listen_port = data["listen"]["port"]
    # Assert
    assert listen_port == 7878


def test_collect_listen_row_carries_pidfile_path(db: Path, tmp_path: Path) -> None:
    # Arrange
    data = collect_ports_data(db_path=db, listen_port=7878, lock_dir=tmp_path)
    # Act
    pidfile = data["listen"]["pidfile"]
    # Assert
    assert pidfile.endswith("listen-7878.pid")


# ---------------------------------------------------------------------------
# collect_ports_data — a2a claims
# ---------------------------------------------------------------------------


def test_collect_lists_a2a_claim_owner(db: Path, free_port: int) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=free_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=7878)
    # Act
    owners = {row["owner"] for row in data["a2a_claims"]}
    # Assert
    assert "alpha" in owners


def test_collect_marks_live_listening_port_as_live(
    db: Path, listening_port: int
) -> None:
    # Arrange — claim the very port a real socket is listening on.
    pa.claim_port("beta", explicit=listening_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=7878, probe_timeout=1.0)
    # Act
    row = next(r for r in data["a2a_claims"] if r["owner"] == "beta")
    # Assert
    assert row["live"] is True


def test_collect_marks_dead_claim_as_orphan(db: Path, free_port: int) -> None:
    # Arrange — claim a released port; nothing listens on it.
    pa.claim_port("gamma", explicit=free_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=7878, probe_timeout=0.2)
    # Act
    row = next(r for r in data["a2a_claims"] if r["owner"] == "gamma")
    # Assert
    assert row["orphan"] is True


def test_collect_lists_dead_claim_in_orphans_section(db: Path, free_port: int) -> None:
    # Arrange
    pa.claim_port("gamma", explicit=free_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=7878, probe_timeout=0.2)
    # Act
    orphan_agents = {o["agent"] for o in data["orphans"]}
    # Assert
    assert "gamma" in orphan_agents


# ---------------------------------------------------------------------------
# collect_ports_data — conflict detection
# ---------------------------------------------------------------------------


def test_collect_flags_conflict_when_agent_claims_listen_port(
    db: Path, free_port: int
) -> None:
    # Arrange — an agent claims the same port sac listen is told to use.
    pa.claim_port("clash", explicit=free_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=free_port, probe_timeout=0.2)
    # Act
    conflict_ports = {c["port"] for c in data["conflicts"]}
    # Assert
    assert free_port in conflict_ports


def test_collect_no_conflict_for_disjoint_ports(db: Path, free_port: int) -> None:
    # Arrange — claim differs from the listen port.
    pa.claim_port("solo", explicit=free_port, db_path=db)
    data = collect_ports_data(db_path=db, listen_port=7878, probe_timeout=0.2)
    # Act
    conflicts = data["conflicts"]
    # Assert
    assert conflicts == []


# ---------------------------------------------------------------------------
# reference map
# ---------------------------------------------------------------------------


def test_reference_map_includes_listen_7878() -> None:
    # Arrange
    ranges = {row["range"] for row in _reference_map()}
    # Act
    has_listen = "7878" in ranges
    # Assert
    assert has_listen


def test_reference_map_includes_gui_dashboard_block() -> None:
    # Arrange
    ranges = [row["range"] for row in _reference_map()]
    # Act
    has_gui_block = any("3129" in r for r in ranges)
    # Assert
    assert has_gui_block


def test_reference_map_includes_a2a_range() -> None:
    # Arrange
    purposes = " ".join(row["purpose"] for row in _reference_map())
    # Act
    has_a2a = "a2a" in purposes
    # Assert
    assert has_a2a


# ---------------------------------------------------------------------------
# CLI surface (real Click command via CliRunner)
# ---------------------------------------------------------------------------


def test_cli_json_output_has_listen_key(home_in_tmp) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    payload = json.loads(result.output)
    # Assert
    assert "listen" in payload


def test_cli_json_output_has_reference_section(home_in_tmp) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    payload = json.loads(result.output)
    # Assert
    assert isinstance(payload["reference"], list) and payload["reference"]


def test_cli_human_render_exits_zero(home_in_tmp) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--timeout", "0.1"])
    # Assert
    assert result.exit_code == 0


def test_cli_json_includes_seeded_a2a_claim(home_in_tmp) -> None:
    # Arrange — seed a claim in the HOME-isolated default state.db, then
    # invoke the real CLI (which reads that same default db).
    pa.claim_port("cli-agent", range_=(20000, 20050))
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    owners = {row["owner"] for row in json.loads(result.output)["a2a_claims"]}
    # Assert
    assert "cli-agent" in owners


def test_main_help_lists_ports_command() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert "ports" in result.output
