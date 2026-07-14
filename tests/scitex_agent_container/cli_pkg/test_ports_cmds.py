"""Tests for ``cli_pkg.ports_cmds`` — the ``sac ports`` inventory.

PA-306 no-mocks: every collaborator is real.

* ``CliRunner`` invokes the real Click command.
* A real ``state.db`` under ``tmp_path`` carries real a2a claims made
  through :mod:`_state.port_allocator` — the same allocator production
  uses.
* Liveness is exercised against REAL sockets: a bound-and-listening
  socket (live) and a bound-then-closed free port (dead / orphan). No
  probe is monkeypatched.
* A yield-based ``isolated_state`` fixture redirects BOTH state
  read-paths — ``$HOME`` and the import-time
  ``state_db.DEFAULT_DB_PATH`` constant — at an isolated ``tmp_path``,
  so the CLI smoke tests never read or write the live fleet registry
  (no monkeypatch; these are the codebase's own seams).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state import port_allocator as pa
from scitex_agent_container._state import state_db
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
def free_port():
    """A port that was bound then released — almost certainly nothing
    listens on it, so a probe reports it dead (orphan).

    The socket is acquired inside a ``with`` block, so the fd is closed
    even if ``bind()`` raises, and the fixture ``yield``s (rather than
    ``return``s) per STX-TQ005: a fixture that acquires an external
    resource owns its teardown.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Closed on block exit — the point of the fixture is a port number
    # with nothing listening behind it.
    yield port


@pytest.fixture
def isolated_state(tmp_path):
    """Isolate EVERY read-path the bare CLI consults for state.

    ``sac ports`` takes no ``--db``: it resolves state.db from
    :data:`state_db.DEFAULT_DB_PATH`, a **module-level constant computed
    at import time**. So overriding ``$HOME`` alone does NOT redirect it
    — by the time a fixture runs, the constant already points at the
    developer's real ``~/.scitex/agent-container/runtime/state.db``, and
    a CLI test would *read* (and ``claim_port`` would *WRITE*) the live
    fleet registry. In CI that silently invents a registry; on a real
    host it pollutes one.

    So touch both read-paths — the env var AND the constant — exactly as
    ``tests/smoke/conftest.py::comms_env`` does. These are the seams the
    codebase itself exposes for this: no monkeypatch, no mock.
    """
    db = tmp_path / "state.db"
    prior = {
        k: os.environ.get(k)
        for k in ("HOME", "USERPROFILE", "SCITEX_AGENT_CONTAINER_STATE_DB")
    }
    prior_db_path = state_db.DEFAULT_DB_PATH

    os.environ["HOME"] = str(tmp_path)
    os.environ["USERPROFILE"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield tmp_path
    finally:
        state_db.DEFAULT_DB_PATH = prior_db_path
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


def test_cli_json_output_has_listen_key(isolated_state) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    payload = json.loads(result.output)
    # Assert
    assert "listen" in payload


def test_cli_json_output_has_reference_section(isolated_state) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    payload = json.loads(result.output)
    # Assert
    assert isinstance(payload["reference"], list) and payload["reference"]


def test_cli_human_render_exits_zero(isolated_state) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--timeout", "0.1"])
    # Assert
    assert result.exit_code == 0


def test_cli_json_includes_seeded_a2a_claim(isolated_state) -> None:
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
