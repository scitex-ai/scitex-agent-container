"""Heartbeat-lease fallback rows for fleet list."""

from __future__ import annotations

from scitex_agent_container.cli_pkg._helpers._agent_list_heartbeat_rows import (
    heartbeat_lease_rows,
    overlay_authoritative_heartbeats,
)


def _beat(**overrides):
    beat = {
        "agent_id": "scholar",
        "spec_id": "sha256:spec",
        "host": "compute-04",
        "runtime": "tui",
        "harness": "hermes",
        "engine": "vllm",
        "model": "qwen3-coder",
        "session_id": "session-1",
        "boot_id": "boot-1",
        "seq": 1,
        "monotonic_ns": 10,
        "observed_at": 100.0,
        "progress_at": 100.0,
        "progress_seq": 1,
        "state": "idle",
        "lease_expires_at": 130.0,
        "card_id": "",
        "card_role": "",
        "_process_alive": None,
        "_federation_connected": True,
    }
    beat.update(overrides)
    return beat


def test_live_lease_adds_missing_resident_with_runtime_identity() -> None:
    # Arrange
    # Act
    rows = heartbeat_lease_rows(
        covered=set(),
        display_host="display",
        running_only=True,
        host_display_for=lambda host, _display: host,
        beats=[_beat()],
        now=101.0,
    )
    # Assert
    assert (
        len(rows),
        rows[0]["name"],
        rows[0]["status"],
        rows[0]["engine"],
        rows[0]["model"],
        rows[0]["host"],
    ) == (1, "scholar", "running", "vllm", "qwen3-coder", "compute-04")


def test_expired_lease_is_not_in_running_only_view() -> None:
    # Arrange
    # Act
    rows = heartbeat_lease_rows(
        covered=set(),
        display_host="display",
        running_only=True,
        host_display_for=lambda host, _display: host,
        beats=[_beat(lease_expires_at=90.0)],
        now=101.0,
    )
    # Assert
    assert rows == []


def test_registry_or_instance_row_keeps_precedence() -> None:
    # Arrange
    # Act
    rows = heartbeat_lease_rows(
        covered={"scholar"},
        display_host="display",
        running_only=False,
        host_display_for=lambda host, _display: host,
        beats=[_beat()],
        now=101.0,
    )
    # Assert
    assert rows == []


def test_host_process_evidence_can_classify_dead() -> None:
    # Arrange
    beat = _beat(_process_alive=False, _federation_connected=True)
    # Act
    rows = heartbeat_lease_rows(
        covered=set(),
        display_host="display",
        running_only=False,
        host_display_for=lambda host, _display: host,
        beats=[beat],
        now=101.0,
    )
    # Assert
    assert (rows[0]["status"], rows[0]["labels"]["resident_state"]) == (
        "unknown",
        "dead",
    )


def test_observer_only_hermes_row_stays_explicitly_unknown() -> None:
    # Arrange
    rows = [{"name": "scholar", "harness": "hermes", "status": "running"}]
    # Act
    overlay_authoritative_heartbeats(rows, beats=[], now=101.0)
    # Assert
    assert (rows[0]["resident_state"], rows[0]["heartbeat_authoritative"]) == (
        "unknown",
        False,
    )


def test_native_hermes_beat_overlays_progress_on_existing_row() -> None:
    # Arrange
    rows = [{"name": "scholar", "harness": "hermes", "status": "unknown"}]
    # Act
    overlay_authoritative_heartbeats(rows, beats=[_beat()], now=101.0)
    # Assert
    assert (
        rows[0]["resident_state"],
        rows[0]["heartbeat_authoritative"],
        rows[0]["heartbeat_boot_id"],
        rows[0]["heartbeat_progress_seq"],
        rows[0]["status"],
    ) == ("idle", True, "boot-1", 1, "running")
