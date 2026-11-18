"""Heartbeat-lease column mapping on ``instances`` rows.

Measured 2026-11-17 on compute-01..04: ``sac listen`` logged
``diary instance-heartbeat write failed ... KeyError: 'nonce_challenge'``
on every beat (306,200 consecutive on compute-03). A writer that is not
enrolled in the nonce protocol omits the nonce pair by spec, and the
row builder indexed every column as if it were required, so every
unenrolled lease renewal was dropped.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.authoritative_heartbeat import validate_heartbeat
from scitex_agent_container._state.state_store_instances_heartbeat import (
    _heartbeat_columns,
)


def _unenrolled_beat() -> dict[str, object]:
    """The resident beat ``_session_beat`` publishes with no challenge enrolled."""
    beat = {
        "agent_id": "scholar",
        "spec_id": "sha256:spec",
        "host": "compute-03",
        "runtime": "tui",
        "harness": "hermes",
        "engine": "vllm",
        "model": "qwen",
        "session_id": "session-1",
        "boot_id": "boot-1",
        "progress_at": 99.0,
        "session_jsonl_delta_bytes": 0.0,
        "subagent_jsonl_delta_bytes": 0.0,
        "seq": 1,
        "monotonic_ns": 10,
        "observed_at": 100.0,
        "state": "idle",
        "lease_expires_at": 190.0,
        "card_id": "",
        "card_role": "",
    }
    return validate_heartbeat(
        beat, expected_agent="scholar", expected_host="compute-03", now=100.0
    )


def test_unenrolled_beat_maps_without_nonce_and_clears_the_columns() -> None:
    # Arrange
    validated = _unenrolled_beat()
    # Act
    columns = _heartbeat_columns(validated)
    # Assert
    assert (
        columns["heartbeat_nonce_challenge"],
        columns["heartbeat_nonce_echo"],
        columns["heartbeat_seq"],
    ) == (None, None, 1)


def test_enrolled_beat_maps_its_nonce_pair() -> None:
    # Arrange
    validated = {
        **_unenrolled_beat(),
        "nonce_challenge": "0123456789012345",
        "nonce_echo": "0123456789012345",
    }
    # Act
    columns = _heartbeat_columns(validated)
    # Assert
    assert (
        columns["heartbeat_nonce_challenge"],
        columns["heartbeat_nonce_echo"],
    ) == ("0123456789012345", "0123456789012345")


def test_a_missing_required_field_is_still_a_loud_error() -> None:
    # Arrange
    validated = _unenrolled_beat()
    del validated["boot_id"]

    # Act
    def build():
        return _heartbeat_columns(validated)

    # Assert
    with pytest.raises(KeyError, match="boot_id"):
        build()


def _full_beat(*, agent: str, host: str, now: float) -> dict[str, object]:
    """A beat carrying the work evidence AND the nonce pair.

    These four fields were mapped to columns neither schema declared, so
    every such beat raised ``KeyError: 'heartbeat_session_jsonl_delta_bytes'``
    inside ``Store.put`` (measured 2026-11-18, compute-01/03/04).
    """
    return {
        "agent_id": agent,
        "spec_id": "sha256:spec",
        "host": host,
        "runtime": "tui",
        "harness": "claude-code",
        "engine": "anthropic",
        "model": "opus",
        "session_id": "session-1",
        "boot_id": "boot-1",
        "seq": 1,
        "monotonic_ns": 10,
        "observed_at": now,
        "progress_at": now - 1.0,
        "session_jsonl_delta_bytes": 4096,
        "subagent_jsonl_delta_bytes": 512.0,
        "nonce_challenge": "0123456789012345",
        "nonce_echo": "0123456789012345",
        "state": "active",
        "lease_expires_at": now + 90.0,
        "card_id": "",
        "card_role": "",
    }


def _record_full_beat() -> str:
    """Start ``alpha`` on this host and record one full beat; return its id."""
    import time

    from scitex_agent_container._state.state_store_hostname import resolve_host
    from scitex_agent_container._state.state_store_instances import (
        record_instance_heartbeat,
        record_instance_start,
    )

    instance_id = record_instance_start("alpha")
    beat = _full_beat(agent="alpha", host=resolve_host(None), now=time.time())
    assert record_instance_heartbeat(instance_id, beat) is True
    return instance_id


def test_a_full_heartbeat_records_every_field_on_the_row(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.state_store_instances import read_instance

    # Act
    row = read_instance(_record_full_beat())
    # Assert
    assert (
        row["heartbeat_session_jsonl_delta_bytes"],
        row["heartbeat_subagent_jsonl_delta_bytes"],
        row["heartbeat_nonce_challenge"],
        row["heartbeat_nonce_echo"],
    ) == (4096.0, 512.0, "0123456789012345", "0123456789012345")


def test_a_full_heartbeat_reads_back_as_the_previous_beat(pg_schema: str) -> None:
    # Arrange — the next renewal validates against the stored row, so what
    # was written must come back under the heartbeat field names.
    from scitex_agent_container._state.state_store_instances import read_instance
    from scitex_agent_container._state.state_store_instances_heartbeat import (
        _heartbeat_from_instance,
    )

    instance_id = _record_full_beat()
    # Act
    previous = _heartbeat_from_instance(read_instance(instance_id))
    # Assert
    assert (
        previous["session_jsonl_delta_bytes"],
        previous["nonce_echo"],
        previous["card_id"],
    ) == (4096.0, "0123456789012345", "")
