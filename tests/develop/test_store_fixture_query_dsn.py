"""Scoped PostgreSQL fixtures preserve the configured connection options."""

import os

import psycopg

from tests._store_isolation import PG_BASE_DSN


def test_scoped_fixture_preserves_base_connection_settings(pg_schema):
    # Arrange
    query = "SELECT current_setting('statement_timeout'), current_setting('application_name')"
    with psycopg.connect(PG_BASE_DSN) as base:
        expected = base.execute(query).fetchone()
    # Act
    with psycopg.connect(os.environ["SCITEX_STORE_DSN"]) as scoped:
        actual = scoped.execute(query).fetchone()
        schema = scoped.execute("SELECT current_schema()").fetchone()[0]
    # Assert
    assert (schema, actual) == (pg_schema, expected)
