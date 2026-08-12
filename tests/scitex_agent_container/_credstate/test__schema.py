"""The shipped schema, and the invariants its shape is supposed to carry."""

from __future__ import annotations

import pytest

from scitex_agent_container._credstate._contract import (
    CONFLICT_CLASSES,
    parse_columns,
    strip_comments,
)
from scitex_agent_container._credstate._model import (
    ROLE_PRIMARY,
    ROLE_REPLICA,
    TIER_DISTRIBUTABLE,
    TIER_PRIMARY_SECRET,
)
from scitex_agent_container._credstate._schema import (
    CR001_MULTIPLE_PRIMARIES_SQL,
    DESCRIPTOR_DDL,
    DIVERGENT_DECLARATIONS_SQL,
    OBSERVATION_DDL,
    PLACEMENT_DDL,
    TABLES,
    assert_schema_contract,
)


def test_the_shipped_schema_satisfies_the_sync_contract():
    # Arrange — fails closed, so this is the gate on the whole domain.
    # Act
    result = assert_schema_contract()
    # Assert
    assert result is None


@pytest.mark.parametrize("name,ddl,conflict_class", TABLES)
def test_every_table_declares_a_known_conflict_class(name, ddl, conflict_class):
    # Arrange
    known = set(CONFLICT_CLASSES)
    # Act
    declared = conflict_class
    # Assert
    assert declared in known


def test_the_observation_table_puts_origin_node_in_its_primary_key():
    # Arrange — this is what makes append-only union collision-free.
    body = strip_comments(OBSERVATION_DDL)
    # Act
    key_line = [ln for ln in body.splitlines() if "PRIMARY KEY" in ln][0]
    # Assert
    assert "origin_node" in key_line


def test_the_descriptor_carries_a_row_revision():
    # Arrange
    columns = parse_columns(DESCRIPTOR_DDL)
    # Act
    present = "revision" in columns
    # Assert
    assert present


def test_the_descriptor_carries_a_material_generation_distinct_from_revision():
    # Arrange — conflating them makes a typo fix look like a rotation.
    columns = parse_columns(DESCRIPTOR_DDL)
    # Act
    present = "generation" in columns
    # Assert
    assert present


def test_the_descriptor_restricts_tier_to_the_two_defined_tiers():
    # Arrange
    ddl = strip_comments(DESCRIPTOR_DDL)
    # Act
    constrained = TIER_PRIMARY_SECRET in ddl and TIER_DISTRIBUTABLE in ddl
    # Assert
    assert constrained


def test_the_placement_restricts_role_to_primary_or_replica():
    # Arrange
    ddl = strip_comments(PLACEMENT_DDL)
    # Act
    constrained = ROLE_PRIMARY in ddl and ROLE_REPLICA in ddl
    # Assert
    assert constrained


def test_the_placement_records_a_locator_rather_than_material():
    # Arrange
    columns = parse_columns(PLACEMENT_DDL)
    # Act
    present = "locator" in columns
    # Assert
    assert present


def test_no_table_declares_a_column_that_could_hold_material():
    # Arrange — a structural check on the schema itself, not on rows.
    forbidden = {"token", "secret", "password", "refresh_token", "access_token"}
    # Act
    declared = {c for _n, ddl, _k in TABLES for c in parse_columns(ddl)}
    # Assert
    assert declared & forbidden == set()


def test_the_cr001_query_groups_by_credential():
    # Arrange
    sql = CR001_MULTIPLE_PRIMARIES_SQL
    # Act
    shaped = "HAVING" in sql and "primary_node" in sql
    # Assert
    assert shaped


def test_the_cr001_query_reads_the_descriptor_table():
    # Arrange
    sql = CR001_MULTIPLE_PRIMARIES_SQL
    # Act
    target = "credential_descriptor" in sql
    # Assert
    assert target


def test_the_cr001_query_ignores_tombstoned_rows():
    # Arrange — a retired declaration must not raise a false alarm.
    sql = CR001_MULTIPLE_PRIMARIES_SQL
    # Act
    filtered = "deleted_at IS NULL" in sql
    # Assert
    assert filtered


def test_the_divergence_query_detects_two_origins_declaring_one_credential():
    # Arrange
    sql = DIVERGENT_DECLARATIONS_SQL
    # Act
    shaped = "HAVING" in sql and "origin_node" in sql
    # Assert
    assert shaped
