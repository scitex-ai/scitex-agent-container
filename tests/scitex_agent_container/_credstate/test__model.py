"""Row types mint their own identity and cannot carry material.

The model/schema drift test is the important one here: a dataclass field
the DDL does not have produces an INSERT that fails only at runtime, on
the host that happened to write first.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from scitex_agent_container._credstate._contract import parse_columns
from scitex_agent_container._credstate._material import CredentialMaterialError
from scitex_agent_container._credstate._model import (
    CredentialDescriptor,
    CredentialObservation,
    CredentialPlacement,
)
from scitex_agent_container._credstate._schema import (
    DESCRIPTOR_DDL,
    OBSERVATION_DDL,
    PLACEMENT_DDL,
)

FAKE_ANTHROPIC = "sk-ant-" + "A" * 40

_PAIRS = [
    (CredentialDescriptor, DESCRIPTOR_DDL),
    (CredentialPlacement, PLACEMENT_DDL),
    (CredentialObservation, OBSERVATION_DDL),
]


@pytest.mark.parametrize("cls,ddl", _PAIRS)
def test_each_row_type_matches_its_tables_column_set(cls, ddl):
    # Arrange — pins model/schema drift, which otherwise surfaces as a
    # runtime INSERT failure on whichever host writes first.
    row = cls(origin_node="n", cred_key="k")
    # Act
    columns = set(parse_columns(ddl))
    # Assert
    assert set(row.to_row()) == columns


@pytest.mark.parametrize("cls,ddl", _PAIRS)
def test_each_row_type_mints_a_distinct_row_uuid(cls, ddl):
    # Arrange
    first = cls(origin_node="n", cred_key="k")
    # Act
    second = cls(origin_node="n", cred_key="k")
    # Assert
    assert first.row_uuid != second.row_uuid


@pytest.mark.parametrize("cls,ddl", _PAIRS)
def test_each_row_type_starts_at_revision_one(cls, ddl):
    # Arrange
    row = cls(origin_node="n", cred_key="k")
    # Act
    revision = row.revision
    # Assert
    assert revision == 1


@pytest.mark.parametrize("cls,ddl", _PAIRS)
def test_updated_at_is_timezone_aware_utc(cls, ddl):
    # Arrange — a naive timestamp cannot be compared across hosts.
    row = cls(origin_node="n", cred_key="k")
    # Act
    offset = row.updated_at.utcoffset()
    # Assert
    assert offset == timezone.utc.utcoffset(None)


def test_a_descriptor_carrying_material_cannot_become_a_row():
    # Arrange
    row = CredentialDescriptor(origin_node="n", cred_key="k", note=FAKE_ANTHROPIC)
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        row.to_row()


def test_a_placement_carrying_material_cannot_become_a_row():
    # Arrange
    row = CredentialPlacement(
        origin_node="n", cred_key="k", node="n", locator="file:/x", note=FAKE_ANTHROPIC
    )
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        row.to_row()


def test_an_observation_carrying_material_cannot_become_a_row():
    # Arrange
    row = CredentialObservation(
        origin_node="n", cred_key="k", node="n", verdict="OK", detail=FAKE_ANTHROPIC
    )
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        row.to_row()


def test_bumped_increments_the_revision_by_exactly_one():
    # Arrange
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    bumped = row.bumped(note="edited")
    # Assert
    assert bumped.revision == row.revision + 1


def test_bumped_applies_the_change():
    # Arrange
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    bumped = row.bumped(note="edited")
    # Assert
    assert bumped.note == "edited"


def test_bumped_preserves_row_identity():
    # Arrange — a bump is a new version of the SAME row, not a new row.
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    bumped = row.bumped(note="edited")
    # Assert
    assert bumped.row_uuid == row.row_uuid


def test_bumped_preserves_the_authoring_node():
    # Arrange — origin_node is the ownership partition key.
    row = CredentialDescriptor(origin_node="scitex-nas-03", cred_key="k")
    # Act
    bumped = row.bumped(note="edited")
    # Assert
    assert bumped.origin_node == "scitex-nas-03"


def test_tombstoned_sets_the_deletion_stamp():
    # Arrange — rows are never DELETEd (ADR-0022 §5.2 rule 5).
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    dead = row.tombstoned()
    # Assert
    assert dead.deleted_at is not None


def test_tombstoned_keeps_the_row_identity_so_the_delete_can_replicate():
    # Arrange
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    dead = row.tombstoned()
    # Assert
    assert dead.row_uuid == row.row_uuid


def test_tombstoned_bumps_the_revision():
    # Arrange
    row = CredentialDescriptor(origin_node="n", cred_key="k")
    # Act
    dead = row.tombstoned()
    # Assert
    assert dead.revision == row.revision + 1


def test_generation_is_independent_of_revision():
    # Arrange — a metadata edit must not look like a token rotation.
    row = CredentialDescriptor(origin_node="n", cred_key="k", generation=7)
    # Act
    bumped = row.bumped(note="typo fix")
    # Assert
    assert bumped.generation == 7
