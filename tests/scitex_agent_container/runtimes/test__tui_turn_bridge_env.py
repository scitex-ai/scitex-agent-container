"""Project-scoped host environment tests for the TUI turn bridge."""

from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.runtimes._tui_turn_bridge_env import (
    turn_bridge_db_observation,
    turn_bridge_process_env,
)


def _config(*, env=None, raw_args=None):
    return SimpleNamespace(
        name="scitex-hub-signup",
        labels={"project": "scitex-hub"},
        env=env or {},
        apptainer=SimpleNamespace(raw_args=raw_args or []),
    )


def test_cli_pguser_is_replaced_by_the_agent_project_role() -> None:
    # Arrange
    host = {"PGUSER": "ywatanabe__cli", "PGPASSFILE": "/host/.pgpass"}
    # Act
    result = turn_bridge_process_env(_config(), host_environ=host)
    # Assert
    assert (result["PGUSER"], result["PGPASSFILE"]) == (
        "ywatanabe__scitex-hub",
        "/host/.pgpass",
    )


def test_spec_pguser_preserved_but_container_passfile_never_reaches_host() -> None:
    # Arrange
    config = _config(
        env={"PGUSER": "service_role", "PGPASSFILE": "/declared/agent.pgpass"}
    )
    # Act
    result = turn_bridge_process_env(
        config,
        host_environ={"PGUSER": "ywatanabe__cli", "PGPASSFILE": "/host/.pgpass"},
    )
    # Assert
    assert (result["PGUSER"], result["PGPASSFILE"]) == (
        "service_role",
        "/host/.pgpass",
    )


def test_raw_pguser_wins_but_raw_container_passfile_is_ignored() -> None:
    # Arrange
    config = _config(
        env={"PGUSER": "spec_role", "PGPASSFILE": "/spec/pass"},
        raw_args=["--env", "PGUSER=raw_role", "--env=PGPASSFILE=/raw/pass"],
    )
    # Act
    result = turn_bridge_process_env(
        config, host_environ={"HOME": "/host/home", "PGPASSFILE": "/host/pass"}
    )
    # Assert
    assert (result["PGUSER"], result["PGPASSFILE"]) == (
        "raw_role",
        "/host/pass",
    )


def test_host_process_identity_variables_are_not_replaced_by_container_env() -> None:
    # Arrange
    config = _config(
        env={"HOME": "/home/agent", "PATH": "/container/bin", "TMPDIR": "/tmp/in-sif"}
    )
    host = {"HOME": "/home/ywatanabe", "PATH": "/usr/bin", "TMPDIR": "/scratch/tmp"}
    # Act
    result = turn_bridge_process_env(config, host_environ=host)
    # Assert
    assert (result["HOME"], result["PATH"], result["TMPDIR"]) == (
        "/home/ywatanabe",
        "/usr/bin",
        "/scratch/tmp",
    )


def test_hostile_inherited_database_timeouts_are_replaced_by_bounded_policy() -> None:
    # Arrange
    host = {"PGCONNECT_TIMEOUT": "600", "PGOPTIONS": "-c statement_timeout=0"}
    # Act
    result = turn_bridge_process_env(_config(), host_environ=host)
    # Assert
    assert (result["PGCONNECT_TIMEOUT"], result["PGOPTIONS"]) == (
        "5",
        "-c statement_timeout=5000 -c lock_timeout=5000",
    )


def test_identity_observation_exposes_sources_without_environment_secrets() -> None:
    # Arrange
    config = _config(env={"API_TOKEN": "do-not-log-this"})
    process_env = turn_bridge_process_env(
        config,
        host_environ={"PGUSER": "ywatanabe__cli", "PGPASSFILE": "/host/.pgpass"},
    )
    # Act
    observation = turn_bridge_db_observation(
        config,
        process_env,
        host_environ={"PGUSER": "ywatanabe__cli", "PGPASSFILE": "/host/.pgpass"},
    )
    # Assert
    assert observation == {
        "configured_pguser": None,
        "effective_pguser": "ywatanabe__scitex-hub",
        "pgpassfile_source": "host",
        "container_pgpassfile_ignored": "false",
    }
