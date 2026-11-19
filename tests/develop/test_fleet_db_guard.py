"""No test may reach the live fleet database — the guard must recognise it.

LIVES IN tests/develop/ — it pins the behaviour of the suite's own isolation
plugin (tests/_fleet_db_guard.py), which has no src/ counterpart.

The incident (2026-11-19): CI on the self-hosted runners opened ~1,330
connections in ten minutes to the live pooler ``scitex-primary:55432``. These
tests pin the recogniser every layer of the fix relies on, in BOTH directions:
a guard that refused nothing would pass a one-sided test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fleet_db_guard import (
    FleetDatabaseRefused,
    fleet_endpoint_named_by,
    fleet_env_leaks,
    refuse_fleet_dsn,
)

ROOT = Path(__file__).resolve().parents[2]

FLEET_DSNS = [
    "postgresql://scitex-primary:55432/scitex",
    "postgresql://ywatanabe__cli@scitex-primary:55432/scitex",
    "postgresql://cards-primary/scitex",
    "postgresql://100.64.0.5:5432/scitex",
    "postgresql://127.0.0.1:55432/scitex",
    "postgresql://localhost:55433/postgres",
    "postgresql:///scitex?host=/home/me/.scitex/pg/run&port=55432",
    "host=scitex-primary port=55432 dbname=scitex",
    "host=127.0.0.1 port=55433 dbname=scitex",
    "postgresql://127.0.0.1:5432,scitex-primary:55432/scitex",
]

SAFE_DSNS = [
    "postgresql://scitex:scitex@127.0.0.1:49153/scitex",
    "postgresql://127.0.0.1:1/tests_must_not_write_to_the_fleet_store",
    "host=127.0.0.1 port=40001 dbname=postgres",
    "postgresql://127.0.0.1:46313/postgres?options=-csearch_path%3Dsac_test_x",
]


@pytest.mark.parametrize("dsn", FLEET_DSNS)
def test_every_spelling_of_the_fleet_database_is_recognised(dsn):
    # Arrange
    target = dsn
    # Act
    why = fleet_endpoint_named_by(target)
    # Assert
    assert why is not None


@pytest.mark.parametrize("dsn", SAFE_DSNS)
def test_a_throwaway_database_is_not_mistaken_for_the_fleet(dsn):
    # Arrange
    target = dsn
    # Act
    why = fleet_endpoint_named_by(target)
    # Assert
    assert why is None


def test_a_declared_fleet_dsn_is_refused_not_skipped():
    # Arrange
    dsn = "postgresql://scitex-primary:55432/scitex"
    raised: Exception | None = None
    # Act
    try:
        refuse_fleet_dsn(dsn, source="SAC_TEST_PG_DSN")
    except FleetDatabaseRefused as exc:  # a UsageError: pytest stops, never skips
        raised = exc
    # Assert
    assert "LIVE fleet database" in str(raised)


def test_libpq_environment_pointing_at_the_fleet_is_reported():
    # Arrange
    environ = {
        "SCITEX_STORE_DSN": "postgresql://127.0.0.1:1/x",
        "PGHOST": "scitex-primary",
    }
    # Act
    leaks = fleet_env_leaks(environ)
    # Assert
    assert [leak.split("=")[0] for leak in leaks] == ["PGHOST"]


def test_the_plugin_refuses_to_start_when_the_test_dsn_names_the_fleet(tmp_path):
    # Arrange — a real child pytest, so the import-time refusal is exercised
    # exactly as a developer or CI would meet it.
    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_probe():\n    pass\n", encoding="utf-8")
    env = dict(os.environ, SAC_TEST_PG_DSN="postgresql://scitex-primary:55432/scitex")
    # Act
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-p",
            "tests._store_isolation",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    # Assert
    assert (
        result.returncode != 0
        and "LIVE fleet database" in result.stdout + result.stderr
    )
