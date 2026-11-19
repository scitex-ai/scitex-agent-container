"""The one heartbeat declaration, and every consumer that derives from it.

Measured 2026-11-18 on compute-01/03/04: ``sac listen`` logged
``diary instance-heartbeat write failed ... KeyError:
'heartbeat_session_jsonl_delta_bytes'`` on every beat (14,900 consecutive on
compute-03). The writer mapped four fields to columns neither instances
schema declared, because the field list lived in four hand-kept copies. These
tests pin the invariant the fix rests on: whatever the writer can emit, BOTH
schemas declare.
"""

from __future__ import annotations

from scitex_agent_container._state.authoritative_heartbeat import ALLOWED_FIELDS
from scitex_agent_container._state.heartbeat_columns import (
    HEARTBEAT_COLUMN_SPECS,
    HEARTBEAT_COLUMNS,
)
from scitex_agent_container._state.state_store_instances_heartbeat import (
    _heartbeat_columns,
)
from scitex_agent_container._state.state_store_instances_store import (
    HEARTBEAT_LEASE_FIELDS,
    instances_schema,
)
from scitex_agent_container._store_plugin import INSTANCES

# Written by ``record_instance_heartbeat`` beside the mapped block.
_WRITER_EXTRA_COLUMNS = {"last_heartbeat_at"}


def _every_field_beat() -> dict[str, object]:
    """A beat carrying every field validation accepts, optional ones included."""
    return {field: object() for field in ALLOWED_FIELDS if field != "host"}


def _emitted_columns() -> set[str]:
    return set(_heartbeat_columns(_every_field_beat())) | _WRITER_EXTRA_COLUMNS


def test_every_column_the_writer_emits_is_declared_by_the_opener_schema() -> None:
    # Arrange
    declared = set(instances_schema().fields)
    # Act
    undeclared = _emitted_columns() - declared
    # Assert
    assert undeclared == set()


def test_every_column_the_writer_emits_is_declared_by_the_plugin_schema() -> None:
    # Arrange
    declared = set(INSTANCES.fields)
    # Act
    undeclared = _emitted_columns() - declared
    # Assert
    assert undeclared == set()


def test_every_field_validation_accepts_has_a_column() -> None:
    # Arrange — ``host`` is the row identity, not a heartbeat column.
    accepted = ALLOWED_FIELDS - {"host"}
    # Act
    unmapped = accepted - set(HEARTBEAT_COLUMNS)
    # Assert
    assert unmapped == set()


def test_the_four_fields_that_were_dropped_are_typed_as_the_contract_says() -> None:
    # Arrange — FiniteFloat|None deltas are REAL; str|None nonces are TEXT.
    declared = instances_schema().fields
    # Act
    kinds = {
        column: declared[column].kind.name
        for column in (
            "heartbeat_session_jsonl_delta_bytes",
            "heartbeat_subagent_jsonl_delta_bytes",
            "heartbeat_nonce_challenge",
            "heartbeat_nonce_echo",
        )
    }
    # Assert
    assert kinds == {
        "heartbeat_session_jsonl_delta_bytes": "REAL",
        "heartbeat_subagent_jsonl_delta_bytes": "REAL",
        "heartbeat_nonce_challenge": "TEXT",
        "heartbeat_nonce_echo": "TEXT",
    }


def test_every_heartbeat_column_is_an_optional_last_writer_wins_data_field() -> None:
    # Arrange — optional DATA is what additive schema evolution can add to a
    # deployed store; anything else would need a backfill migration.
    declared = instances_schema().fields
    # Act
    off_rule = {
        spec.column
        for spec in HEARTBEAT_COLUMN_SPECS
        if declared[spec.column].merge.name != "LAST_WRITER_WINS"
        or declared[spec.column].role.name != "DATA"
        or declared[spec.column].required
    }
    # Assert
    assert off_rule == set()


def test_every_heartbeat_column_is_gated_as_an_evolving_field() -> None:
    # Arrange — the launch gate and the legacy reader both key on this list.
    columns = {spec.column for spec in HEARTBEAT_COLUMN_SPECS}
    # Act
    ungated = columns - set(HEARTBEAT_LEASE_FIELDS)
    # Assert
    assert ungated == set()
