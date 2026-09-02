"""Tests for ``cli_pkg.ports_cmds`` — the ``sac ports`` inventory.

PA-306 no-mocks: every collaborator is real.

* ``CliRunner`` invokes the real Click command.
* A real PostgreSQL schema (the shared ``pg_schema`` fixture) carries real
  a2a claims made through :mod:`_state.port_allocator` — the same allocator
  production uses. It was a ``state.db`` under ``tmp_path`` until 2026-08-28;
  ``a2a_ports`` moved to per-host PostgreSQL and ``db_path`` went with it,
  from ``collect_ports_data`` as well as from the allocator. The fixture
  points the REAL resolver at a throwaway schema, so this exercises the
  resolution production performs rather than bypassing it — and it SKIPS
  where no writable database exists, which is not a pass.
* Liveness is exercised against REAL sockets: a bound-and-listening
  socket (live) and a bound-then-closed free port (dead / orphan). No
  probe is monkeypatched.
* A yield-based ``isolated_state`` fixture points ``$HOME`` and
  ``$SCITEX_AGENT_CONTAINER_STATE_DB`` at an isolated ``tmp_path``, so the
  CLI smoke tests never read or write the live fleet registry (no
  monkeypatch; these are the codebase's own seams). It also redirected the
  import-time ``state_db.DEFAULT_DB_PATH`` constant until 2026-08-30, when
  that constant was deleted with the storage engine.
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
def dead_claim_port(dead_port):
    """A port with nothing listening on it, so a probe reports it dead (orphan).

    ``collect_ports_data``'s default probe is ``port_is_bound`` — a real
    outbound TCP CONNECT — so a socket bound WITHOUT ``listen()`` answers the
    SYN with RST and is correctly classified dead, exactly like the released
    port this used to hand out.

    Unlike that one, this port is HELD (see the shared ``dead_port`` fixture in
    tests/scitex_agent_container/_helpers/ports.py). Releasing it put the
    number back in the ephemeral pool before the probe ran, so any other test
    or xdist worker could bind it and the "orphan" would report LIVE.

    Renamed from ``free_port``: nothing here wants a port it can bind, and that
    conflation is what the shared helper exists to keep apart.
    """
    return dead_port()


@pytest.fixture
def isolated_state(tmp_path, pg_schema):
    """Isolate EVERY read-path the bare CLI consults for state.

    DEPENDS ON ``pg_schema`` (2026-08-28) because the a2a claims moved to
    PostgreSQL, and the two isolations have to happen in that order. This
    fixture repoints ``$HOME``, which is where libpq looks for ``.pgpass``;
    ``pg_schema`` pins ``PGPASSFILE`` explicitly during ITS setup, so
    requesting it here makes that pinning happen first. Written as a
    dependency rather than left to autouse ordering, for the same reason
    ``_isolate_state_db`` requests ``_assert_state_floor_intact`` by name.

    ``sac ports`` takes no ``--db``. It used to resolve state.db from
    :data:`state_db.DEFAULT_DB_PATH`, a **module-level constant computed at
    import time**, so overriding ``$HOME`` alone did NOT redirect it — by the
    time a fixture ran, the constant already pointed at the developer's real
    ``~/.scitex/agent-container/runtime/state.db``, and a CLI test would
    *read* (and ``claim_port`` would *WRITE*) the live fleet registry. In CI
    that silently invented a registry; on a real host it polluted one.

    THAT CONSTANT WAS DELETED WITH THE STORAGE ENGINE on 2026-08-30, and the
    ledger it addressed had already moved to PostgreSQL. ``pg_schema`` above
    is what isolates the claim now; ``$HOME`` and the env var are still
    pinned here because the CLI reads both. These are the seams the codebase
    itself exposes: no monkeypatch, no mock.
    """
    db = tmp_path / "state.db"
    prior = {
        k: os.environ.get(k)
        for k in ("HOME", "USERPROFILE", "SCITEX_AGENT_CONTAINER_STATE_DB")
    }

    os.environ["HOME"] = str(tmp_path)
    os.environ["USERPROFILE"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
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


def test_collect_reports_listen_port_at_given_bind(pg_schema: str) -> None:
    # Arrange
    data = collect_ports_data(listen_host="127.0.0.1", listen_port=7878)
    # Act
    listen_port = data["listen"]["port"]
    # Assert
    assert listen_port == 7878


def test_collect_listen_row_carries_pidfile_path(pg_schema: str, tmp_path: Path) -> None:
    # Arrange
    data = collect_ports_data(listen_port=7878, lock_dir=tmp_path)
    # Act
    pidfile = data["listen"]["pidfile"]
    # Assert
    assert pidfile.endswith("listen-7878.pid")


# ---------------------------------------------------------------------------
# collect_ports_data — a2a claims
# ---------------------------------------------------------------------------


def test_collect_lists_a2a_claim_owner(pg_schema: str, dead_claim_port: int) -> None:
    # Arrange
    pa.claim_port("alpha", explicit=dead_claim_port)
    data = collect_ports_data(listen_port=7878)
    # Act
    owners = {row["owner"] for row in data["a2a_claims"]}
    # Assert
    assert "alpha" in owners


def test_collect_marks_live_listening_port_as_live(
    pg_schema: str, listening_port: int
) -> None:
    # Arrange — claim the very port a real socket is listening on.
    pa.claim_port("beta", explicit=listening_port)
    data = collect_ports_data(listen_port=7878, probe_timeout=1.0)
    # Act
    row = next(r for r in data["a2a_claims"] if r["owner"] == "beta")
    # Assert
    assert row["live"] is True


def test_collect_marks_dead_claim_as_orphan(pg_schema: str, dead_claim_port: int) -> None:
    # Arrange — claim a HELD, never-listened port; nothing listens on it.
    pa.claim_port("gamma", explicit=dead_claim_port)
    data = collect_ports_data(listen_port=7878, probe_timeout=0.2)
    # Act
    row = next(r for r in data["a2a_claims"] if r["owner"] == "gamma")
    # Assert
    assert row["orphan"] is True


def test_collect_lists_dead_claim_in_orphans_section(
    pg_schema: str, dead_claim_port: int
) -> None:
    # Arrange
    pa.claim_port("gamma", explicit=dead_claim_port)
    data = collect_ports_data(listen_port=7878, probe_timeout=0.2)
    # Act
    orphan_agents = {o["agent"] for o in data["orphans"]}
    # Assert
    assert "gamma" in orphan_agents


# ---------------------------------------------------------------------------
# collect_ports_data — conflict detection
# ---------------------------------------------------------------------------


def test_collect_flags_conflict_when_agent_claims_listen_port(
    pg_schema: str, dead_claim_port: int
) -> None:
    # Arrange — an agent claims the same port sac listen is told to use.
    pa.claim_port("clash", explicit=dead_claim_port)
    data = collect_ports_data(
        listen_port=dead_claim_port, probe_timeout=0.2
    )
    # Act
    conflict_ports = {c["port"] for c in data["conflicts"]}
    # Assert
    assert dead_claim_port in conflict_ports


def test_collect_no_conflict_for_disjoint_ports(pg_schema: str, dead_claim_port: int) -> None:
    # Arrange — claim differs from the listen port.
    pa.claim_port("solo", explicit=dead_claim_port)
    data = collect_ports_data(listen_port=7878, probe_timeout=0.2)
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
    payload = json.loads(result.stdout)
    # Assert
    assert "listen" in payload


def test_cli_json_output_has_reference_section(isolated_state) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["ports", "--json", "--timeout", "0.1"])
    payload = json.loads(result.stdout)
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
    owners = {row["owner"] for row in json.loads(result.stdout)["a2a_claims"]}
    # Assert
    assert "cli-agent" in owners


def test_main_help_lists_ports_command() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert "ports" in result.output
