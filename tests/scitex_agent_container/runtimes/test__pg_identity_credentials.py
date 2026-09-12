"""Least-privilege materialization of project PostgreSQL credentials."""

from __future__ import annotations

import getpass
import json
import stat
from types import SimpleNamespace

from scitex_agent_container.runtimes._pg_identity_credentials import (
    DEFAULT_CONTAINER_PGPASSFILE,
    materialize_project_pgpass,
)
from scitex_agent_container.runtimes._pg_identity_env import PgIdentityCredentialError


def _config(*, name: str = "scitex-hub-signup", project: str = "scitex-hub"):
    return SimpleNamespace(
        name=name,
        labels={"project": project},
        env={"SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex"},
        apptainer=SimpleNamespace(raw_args=[]),
    )


_DEFAULTS = {"SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex"}


def _servers(dsn: str = "postgresql://scitex-primary:55432/scitex"):
    return {
        "scitex-cards": {
            "env": {
                "SCITEX_CARDS_DB": dsn,
                "PGPASSFILE": DEFAULT_CONTAINER_PGPASSFILE,
            }
        }
    }


def test_materialized_passfile_is_private_and_project_role_filtered(tmp_path):
    # Arrange
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(
        "*:*:*:operator__other:other-secret\n"
        f"scitex-primary:55432:scitex:{role}:project-secret\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    home = tmp_path / "home"
    # Act
    [result] = materialize_project_pgpass(
        _config(),
        home_backings=[home],
        servers=_servers(),
        host_environ={"PGPASSFILE": str(source), "USER": "operator"},
        fleet_defaults=_DEFAULTS,
    )
    # Assert
    assert (
        stat.S_IMODE(result.stat().st_mode),
        result.read_text(encoding="utf-8"),
    ) == (
        0o600,
        f"scitex-primary:55432:scitex:{role}:project-secret\n",
    )


def test_materialization_honours_wildcard_targets_and_escaped_fields(tmp_path):
    # Arrange
    source = tmp_path / "host.pgpass"
    role = f"{getpass.getuser()}__scitex-hub"
    wildcard_host = rf"*:55432:\*\:data:{role}:pa\:ss\\word"
    wildcard_database = f"scitex-primary:55432:*:{role}:second-password"
    source.write_text(f"{wildcard_host}\n{wildcard_database}\n", encoding="utf-8")
    source.chmod(0o600)
    config = _config()
    config.env["SCITEX_CARDS_DB"] = "postgresql://scitex-primary:55432/%2A%3Adata"
    home = tmp_path / "home"
    # Act
    [result] = materialize_project_pgpass(
        config,
        home_backings=[home],
        servers=_servers(config.env["SCITEX_CARDS_DB"]),
        host_environ={"PGPASSFILE": str(source), "USER": "operator"},
        fleet_defaults={},
    )
    # Assert
    assert result.read_text(encoding="utf-8") == (
        f"{wildcard_host}\n{wildcard_database}\n"
    )


def test_materialization_covers_data_and_direct_notify_targets(tmp_path):
    # Arrange
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(
        f"scitex-primary:55432:*:{role}:pooled-secret\n"
        f"scitex-primary:55433:scitex:{role}:direct-secret\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    servers = _servers()
    servers["scitex-cards"]["env"]["SCITEX_CARDS_NOTIFY_DSN"] = (
        "postgresql://scitex-primary:55433/scitex"
    )

    # Act
    [result] = materialize_project_pgpass(
        _config(),
        home_backings=[tmp_path / "home"],
        servers=servers,
        host_environ={"PGPASSFILE": str(source)},
        fleet_defaults={},
    )

    # Assert
    assert result.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_missing_credential_removes_stale_materialized_secret(tmp_path):
    # Arrange
    home = tmp_path / "home"
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(f"*:*:*:{role}:stale-secret\n", encoding="utf-8")
    source.chmod(0o600)
    [destination] = materialize_project_pgpass(
        _config(),
        home_backings=[home],
        servers=_servers(),
        host_environ={"PGPASSFILE": str(source)},
        fleet_defaults=_DEFAULTS,
    )
    source.unlink()
    # Act
    try:
        materialize_project_pgpass(
            _config(),
            home_backings=[home],
            servers=_servers(),
            host_environ={"PGPASSFILE": str(source)},
            fleet_defaults=_DEFAULTS,
        )
    except PgIdentityCredentialError as exc:
        message = str(exc)
    else:  # pragma: no cover - assertion reports the missing refusal
        message = ""
    # Assert
    assert (destination.exists(), "stale-secret" in message) == (False, False)


def test_explicit_passfile_wins_and_removes_prior_sac_copy(tmp_path):
    # Arrange
    home = tmp_path / "home"
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(f"*:*:*:{role}:old-secret\n", encoding="utf-8")
    source.chmod(0o600)
    config = _config()
    [destination] = materialize_project_pgpass(
        config,
        home_backings=[home],
        servers=_servers(),
        host_environ={"PGPASSFILE": str(source)},
        fleet_defaults=_DEFAULTS,
    )
    config.apptainer.raw_args = ["--env", "PGPASSFILE=/creds/project.pgpass"]
    # Act
    result = materialize_project_pgpass(
        config,
        home_backings=[home],
        servers=_servers(),
        fleet_defaults=_DEFAULTS,
    )
    # Assert
    assert (result, destination.exists()) == ([], False)


def test_explicit_reserved_path_does_not_delete_operator_replacement(tmp_path):
    # Arrange
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(f"*:*:*:{role}:generated-secret\n", encoding="utf-8")
    source.chmod(0o600)
    config = _config()
    [destination] = materialize_project_pgpass(
        config,
        home_backings=[tmp_path / "home"],
        servers=_servers(),
        host_environ={"PGPASSFILE": str(source)},
        fleet_defaults=_DEFAULTS,
    )
    destination.write_text("operator-owned-replacement\n", encoding="utf-8")
    config.apptainer.raw_args = [
        "--env",
        f"PGPASSFILE={DEFAULT_CONTAINER_PGPASSFILE}",
    ]
    # Act
    result = materialize_project_pgpass(
        config,
        home_backings=[destination.parent],
        servers=_servers(),
        fleet_defaults=_DEFAULTS,
    )
    # Assert
    assert (result, destination.read_text(encoding="utf-8")) == (
        [],
        "operator-owned-replacement\n",
    )


def test_removing_postgres_mcp_cleans_managed_passfile(tmp_path):
    # Arrange
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(f"*:*:*:{role}:managed-secret\n", encoding="utf-8")
    source.chmod(0o600)
    config = _config()
    [destination] = materialize_project_pgpass(
        config,
        home_backings=[tmp_path / "home"],
        servers=_servers(),
        host_environ={"PGPASSFILE": str(source)},
        fleet_defaults=_DEFAULTS,
    )
    # Act
    result = materialize_project_pgpass(
        config,
        home_backings=[destination.parent],
        servers={},
        fleet_defaults=_DEFAULTS,
    )
    # Assert
    assert (result, destination.exists()) == ([], False)


def test_marker_publication_failure_rolls_back_generated_secret(tmp_path):
    # Arrange
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(f"*:*:*:{role}:must-be-rolled-back\n", encoding="utf-8")
    source.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    destination = home / ".sac-pgpass"
    destination.with_name(f".{destination.name}.sha256").mkdir()
    # Act
    try:
        materialize_project_pgpass(
            _config(),
            home_backings=[home],
            servers=_servers(),
            host_environ={"PGPASSFILE": str(source)},
            fleet_defaults=_DEFAULTS,
        )
    except OSError:
        refused = True
    else:  # pragma: no cover - assertion reports missing rollback
        refused = False
    # Assert
    assert (refused, destination.exists()) == (True, False)


def test_errors_and_compiled_identity_metadata_never_contain_password(tmp_path):
    # Arrange
    password = "do-not-leak-this-password"
    role = f"{getpass.getuser()}__scitex-hub"
    source = tmp_path / "host.pgpass"
    source.write_text(
        f"other-primary:55432:scitex:{role}:{password}\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    config = _config()
    # Act
    try:
        materialize_project_pgpass(
            config,
            home_backings=[tmp_path / "home"],
            servers=_servers(),
            host_environ={"PGPASSFILE": str(source), "USER": "operator"},
            fleet_defaults=_DEFAULTS,
        )
    except PgIdentityCredentialError as exc:
        observable = json.dumps(
            {
                "error": str(exc),
                "snapshot": config.env,
                "birth_certificate": vars(config),
            },
            default=str,
        )
    else:  # pragma: no cover - assertion reports the missing refusal
        observable = password
    # Assert
    assert password not in observable
