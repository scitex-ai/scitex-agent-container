"""Tests for ``cli_pkg/_registry_register.py`` — ``sac registry register``.

ADR-0014 + lead-row repair flow. The verb writes a ``comms_nodes`` row
directly so the operator can fix a missing federated entry without
restarting whatever process "owns" it (the lead's mcp channel, a
peer's listen).

ON POSTGRESQL SINCE 2026-08-28. The directory moved to the shared store, so
these tests take ``pg_schema`` and the verb lost its ``--db-path`` override
(there is no file to override). ``--source-host`` survives with a sharpened
meaning: it is no longer STORED, it declares on whose behalf the write is
made, and the conflict check compares it against the record's ``_origin``.
The two ``--source-host`` tests below therefore assert the CONFLICT
behaviour rather than a round-trip of the value.

Real store via ``pg_schema`` + ``click.testing.CliRunner`` — no MagicMock
anywhere. AAA, one assert per test (STX-TQ002 / PA-307), >=3-word names.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._registry_register import registry_register

# ---------------------------------------------------------------------------
# Fixtures — point state.db at a per-test tmp file so we don't touch
# the operator's real registry.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, env_save_restore) -> Path:
    p = tmp_path / "state.db"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(p))
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    yield p
    importlib.reload(mod)


# ---------------------------------------------------------------------------
# Happy path — the row lands in comms_nodes
# ---------------------------------------------------------------------------


def test_registry_register_writes_comms_nodes_row_with_given_name(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    assert result.exit_code == 0 and lookup_comms_node(name="lead") is not None


def test_registry_register_records_host_and_port_verbatim(db_path: Path, pg_schema: str) -> None:
    # Arrange — pin that the verb persists the args the operator typed,
    # not some derived value.
    runner = CliRunner()
    # Act
    runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["host"] == "lead-host" and info["a2a_port"] == 7878


def test_registry_register_reports_this_host_as_the_origin(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — provenance is the store's ``_origin`` now, stamped from the
    # writing node, so a locally-registered entry names THIS host rather
    # than the NULL the old ``source_host`` column carried.
    import socket

    runner = CliRunner()
    # Act
    runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["source_host"] == socket.gethostname()


def test_explicit_source_host_relays_without_claiming_the_name(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — the operator relays a peer's entry from a third host.
    # ``--source-host`` declares that, and the INSERT path has no stored
    # record to compare against, so it must succeed rather than read as a
    # cross-host collision.
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        [
            "--name",
            "peer-2",
            "--host",
            "peer-2.lan",
            "--a2a-port",
            "7878",
            "--source-host",
            "relay-host",
        ],
    )
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    assert result.exit_code == 0 and lookup_comms_node(name="peer-2") is not None


def test_registry_register_after_resolves_via_resolve_node_host(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — the operator-facing contract: after `sac registry register`
    # succeeds, resolve_node_host (the production lookup callers use to
    # route cross-host A2A) must return the row. This guards the seam
    # between the verb and the resolver.
    runner = CliRunner()
    runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Act
    from scitex_agent_container._state.state_db_nodes import resolve_node_host

    info = resolve_node_host(name="lead")
    # Assert
    assert info == {"host": "lead-host", "a2a_port": 7878}


# ---------------------------------------------------------------------------
# Re-registration — idempotent UPSERT, no conflict on same (host, port)
# ---------------------------------------------------------------------------


def test_registry_register_idempotent_when_same_host_and_port(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — second invocation with identical args must NOT exit
    # non-zero and must NOT create a duplicate row.
    runner = CliRunner()
    runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Act
    second = runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "7878"],
    )
    # Assert
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    rows = [r for r in list_comms_nodes() if r["name"] == "lead"]
    assert second.exit_code == 0 and len(rows) == 1


# ---------------------------------------------------------------------------
# Fail-loud — conflict from a different source_host must exit non-zero
# ---------------------------------------------------------------------------


def test_registry_register_exits_nonzero_on_cross_source_conflict(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — first registration claims (host=A, port=7878), second
    # tries to overwrite with (host=B, port=7878) from a DIFFERENT
    # source_host. ADR-0014 says fail-loud; the verb must surface
    # CommsNodeConflictError as a non-zero exit.
    runner = CliRunner()
    runner.invoke(
        registry_register,
        [
            "--name",
            "lead",
            "--host",
            "host-a",
            "--a2a-port",
            "7878",
            "--source-host",
            "src-a",
        ],
    )
    # Act
    result = runner.invoke(
        registry_register,
        [
            "--name",
            "lead",
            "--host",
            "host-b",
            "--a2a-port",
            "7878",
            "--source-host",
            "src-b",
        ],
    )
    # Assert
    assert result.exit_code != 0


def test_registry_register_does_not_overwrite_existing_row_on_conflict(
    db_path: Path, pg_schema: str
) -> None:
    # Arrange — pair with the exit-code test above: the existing row
    # must survive the rejected second invocation untouched.
    runner = CliRunner()
    runner.invoke(
        registry_register,
        [
            "--name",
            "lead",
            "--host",
            "host-a",
            "--a2a-port",
            "7878",
            "--source-host",
            "src-a",
        ],
    )
    runner.invoke(
        registry_register,
        [
            "--name",
            "lead",
            "--host",
            "host-b",
            "--a2a-port",
            "7878",
            "--source-host",
            "src-b",
        ],
    )
    # Act
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    # Assert — original row is intact
    assert info["host"] == "host-a"


# ---------------------------------------------------------------------------
# Input validation — required flags, port>0
# ---------------------------------------------------------------------------


def test_registry_register_requires_name_flag(db_path: Path, pg_schema: str) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        ["--host", "lead-host", "--a2a-port", "7878"],
    )
    # Assert — missing --name is a click UsageError
    assert result.exit_code != 0


def test_registry_register_requires_host_flag(db_path: Path, pg_schema: str) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        ["--name", "lead", "--a2a-port", "7878"],
    )
    # Assert
    assert result.exit_code != 0


def test_registry_register_requires_a2a_port_flag(db_path: Path, pg_schema: str) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host"],
    )
    # Assert
    assert result.exit_code != 0


def test_registry_register_rejects_non_positive_port(db_path: Path, pg_schema: str) -> None:
    # Arrange — register_comms_node enforces a2a_port > 0; the verb
    # surfaces that as a UsageError (exit 2). The 0-port footgun was
    # the EXACT production-bug signature ADR-0014 closed the door on.
    runner = CliRunner()
    # Act
    result = runner.invoke(
        registry_register,
        ["--name", "lead", "--host", "lead-host", "--a2a-port", "0"],
    )
    # Assert
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Wiring — the verb is reachable via `sac registry register`
# ---------------------------------------------------------------------------


def test_registry_register_is_wired_into_registry_group() -> None:
    # Arrange — guard the registry_group wiring (regression: someone
    # adds a new verb file but forgets to add_command it).
    from scitex_agent_container.cli_pkg.registry_group import registry_group

    # Act
    commands = set(registry_group.commands.keys())
    # Assert
    assert "register" in commands
