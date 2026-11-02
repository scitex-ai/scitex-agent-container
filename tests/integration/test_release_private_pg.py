"""Real private PostgreSQL controls for the existing release-required fixture."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = """import os,psycopg
from tests._store_isolation import pg_schema
owned=pg_schema.__wrapped__(None)
try:
    schema=next(owned)
    with psycopg.connect(os.environ['SCITEX_STORE_DSN']) as connection:
        connection.execute('CREATE TABLE release_guard_probe (value integer)')
        connection.execute('INSERT INTO release_guard_probe VALUES (42)')
        value=connection.execute('SELECT value FROM release_guard_probe').fetchone()[0]
        version=connection.execute('SHOW server_version_num').fetchone()[0]
        print('PRIVATE-PG:'+str(value)+':'+version)
finally:
    owned.close()
"""


@pytest.fixture()
def private_pg16():
    from scitex_dev.store.testing import ephemeral_cluster_dsn

    binary = Path("/usr/lib/postgresql/16/bin/postgres")
    if not binary.is_file():
        pytest.fail("The declared release guard integration requires PostgreSQL 16")
    previous = os.environ.get("PATH")
    os.environ["PATH"] = str(binary.parent) + os.pathsep + (previous or os.defpath)
    try:
        with ephemeral_cluster_dsn() as dsn:
            socket = parse_qs(urlsplit(dsn).query)["host"][0]
            yield "postgresql://postgres@" + quote(socket, safe="") + "/postgres"
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


def probe(dsn, *, user="postgres"):
    env = {
        "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "SAC_TEST_PG_DSN": dsn, "SAC_TEST_PG_REQUIRED": "1",
        "PGUSER": user, "PGPASSFILE": "/nonexistent-release-guard-pgpass",
    }
    return subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=15)


def test_owned_pg16_runs_actual_schema_and_data_operations(private_pg16):
    # Arrange
    dsn = private_pg16
    # Act
    result = probe(dsn)
    # Assert
    assert result.returncode == 0 and "PRIVATE-PG:42:16" in result.stdout, result.stderr


def test_release_required_unavailable_private_endpoint_fails_instead_of_skipping(tmp_path):
    # Arrange: an owned absent Unix endpoint, never a fleet/loopback database.
    dsn = "postgresql://postgres@" + quote(str(tmp_path / "absent"), safe="") + "/postgres"
    # Act
    result = probe(dsn)
    # Assert
    assert result.returncode != 0 and "PostgreSQL fixture cannot use" in result.stderr


def test_release_required_read_only_private_server_is_refused(private_pg16):
    # Arrange: change only this throwaway cluster's role default.
    with psycopg.connect(private_pg16, autocommit=True) as connection:
        connection.execute("ALTER ROLE postgres SET default_transaction_read_only = on")
    # Act
    result = probe(private_pg16)
    # Assert
    assert result.returncode != 0 and "read-only" in result.stderr
