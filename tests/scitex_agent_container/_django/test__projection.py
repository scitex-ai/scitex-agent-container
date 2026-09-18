"""Tests for ``_django/_projection`` (row/detail projection + typed errors).

Pure-function coverage — no HTTP, no mocks, no monkeypatch. Each test has AAA
markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

import socket

from scitex_agent_container._django._projection import project_detail, project_row
from scitex_agent_container._django._remote import RemoteOperationError

LOCAL = socket.gethostname() or "this-node"
REMOTE = "remote-node-" + (LOCAL[:8] or "x")

ALIVE = {"name": "alpha", "liveness": {"verdict": "ALIVE"}, "status": "running",
         "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
         "model": "sonnet", "billing_mode": "subscription",
         "auth_identity": "anthropic/team-max", "runtime_identity_source": "birth_certificate",
         "pid": 111, "session_id": "a" * 32,
         "a2a_port": 19000, "turn_url": f"http://{LOCAL}:19000/v1/turn", "inbox_reachable": "true"}
DEAD = {"name": "beta", "liveness": {"verdict": "DEAD"}, "status": "stopped",
        "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
        "model": "haiku", "pid": 222, "session_id": "b" * 32,
        "a2a_port": 19001, "turn_url": f"http://{LOCAL}:19001/v1/turn", "inbox_reachable": "unknown"}
GAMMA = {"name": "gamma", "liveness": {"verdict": "ALIVE"}, "status": "running",
         "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
         "model": "sonnet", "pid": 333, "session_id": "c" * 32,
         "a2a_port": 19002, "turn_url": f"http://{REMOTE}:19002/v1/turn", "inbox_reachable": "true"}


def test_row_uses_liveness_verdict():
    # Arrange
    row = {"name": "alpha", "turn_url": f"http://{LOCAL}:19000/v1/turn"}
    # Act
    projected = project_row(row, ALIVE)
    # Assert
    assert projected["state_label"] == "Alive"


def test_row_alive_is_good_tone():
    # Arrange
    row = {"name": "alpha"}
    # Act
    tone = project_row(row, ALIVE)["state_tone"]
    # Assert
    assert tone == "good"


def test_stale_latch_is_distinct_from_alive_idle_state():
    # Arrange
    status = {
        **ALIVE,
        "runtime_control": {
            "turn_admission": "stale_latched",
            "detail": "provider stale circuit breaker latched after 5 attempts",
        },
    }
    # Act
    projected = project_row({"name": "alpha"}, status)
    # Assert
    assert (
        projected["state_label"],
        projected["state_tone"],
        projected["state_detail"],
    ) == (
        "Provider stale-latched",
        "warn",
        "provider stale circuit breaker latched after 5 attempts",
    )


def test_dead_liveness_outranks_persisted_stale_latch():
    # Arrange
    status = {
        "status": "stopped",
        "liveness_verdict": "dead",
        "runtime_control": {
            "turn_admission": "stale_latched",
            "detail": "old marker",
        },
    }
    # Act
    projected = project_row({"name": "dead-agent"}, status)
    # Assert
    assert (projected["state_label"], projected["state_tone"], projected["state_detail"]) == (
        "Dead",
        "bad",
        "",
    )


def test_not_started_liveness_outranks_persisted_recovery():
    # Arrange
    status = {
        "status": "not_started",
        "runtime_control": {
            "turn_admission": "recovering",
            "detail": "old marker",
        },
    }
    # Act
    projected = project_row({"name": "new-agent"}, status)
    # Assert
    assert (projected["state_label"], projected["state_tone"], projected["state_detail"]) == (
        "Not started",
        "muted",
        "",
    )


def test_row_dead_agent_is_bad_tone():
    # Arrange
    row = {"name": "beta"}
    # Act
    projected = project_row(row, DEAD)
    # Assert
    assert projected["state_label"] == "Dead" and projected["state_tone"] == "bad"


def test_row_missing_status_is_unknown():
    # Arrange
    row = {"name": "ghost"}
    # Act
    projected = project_row(row, {})
    # Assert
    assert projected["state_label"] == "Unknown"


def test_row_status_exception_is_unknown_not_crash():
    # Arrange
    row = {"name": "alpha"}
    # Act
    projected = project_row(row, Exception("boom"))
    # Assert
    assert projected["state_label"] == "Unknown" and projected["name"] == "alpha"


def test_row_carries_runtime_harness_model():
    # Arrange
    row = {"name": "alpha", "turn_url": f"http://{LOCAL}:19000/v1/turn"}
    # Act
    projected = project_row(row, ALIVE)
    # Assert
    assert (projected["runtime"], projected["harness"], projected["model"]) == ("apptainer", "anthropic", "sonnet")


def test_row_carries_billing_auth_and_identity_provenance():
    # Arrange
    row = {"name": "alpha"}

    # Act
    projected = project_row(row, ALIVE)

    # Assert
    assert (
        projected["billing_mode"],
        projected["auth_identity"],
        projected["runtime_identity_source"],
    ) == ("subscription", "anthropic/team-max", "birth_certificate")


def test_status_birth_identity_outranks_stale_list_row():
    # Arrange
    stale_row = {
        "name": "alpha",
        "harness": "stale-harness",
        "engine": "stale-engine",
        "model": "stale-model",
        "billing_mode": "unspecified",
        "auth_identity": "unknown",
        "runtime_identity_source": "spec",
    }

    # Act
    projected = project_row(stale_row, ALIVE)

    # Assert
    assert (
        projected["harness"],
        projected["engine"],
        projected["model"],
        projected["auth_identity"],
        projected["runtime_identity_source"],
    ) == (
        "anthropic",
        "anthropic",
        "sonnet",
        "anthropic/team-max",
        "birth_certificate",
    )


def test_row_projects_activity_and_keeps_unknown_explicit():
    # Arrange
    status = {
        **ALIVE,
        "activity": {
            "operation": {
                "state": "observed",
                "value": "busy",
                "source": "heartbeat.state",
            },
            "tool": {
                "state": "unknown",
                "reason": "No authoritative runtime evidence is published.",
            },
            "private": {"state": "observed", "value": "secret"},
        },
    }
    # Act
    activity = project_row({"name": "alpha"}, status)["activity"]
    # Assert
    assert (
        activity["operation"]["value"],
        activity["tool"]["state"],
        "private" in activity,
    ) == ("busy", "unknown", False)


def test_row_host_from_turn_url():
    # Arrange
    row = {"name": "alpha", "turn_url": f"http://{LOCAL}:19000/v1/turn"}
    # Act
    host = project_row(row, ALIVE)["host"]
    # Assert
    assert host == LOCAL


def test_row_role_list_is_joined():
    # Arrange
    row = {"name": "x", "role": ["a", "b"]}
    # Act
    role = project_row(row, ALIVE)["role"]
    # Assert
    assert role == "a, b"


def test_cross_host_row_is_tagged():
    # Arrange
    row = {"name": "gamma", "scope": "cross-host", "turn_url": f"http://{REMOTE}:19002/v1/turn"}
    # Act
    projected = project_row(row, GAMMA)
    # Assert
    assert projected["cross_host"] is True and projected["host"] == REMOTE


def test_detail_includes_short_session():
    # Arrange
    row = {"name": "alpha", "started_at": "T0", "turn_url": f"http://{LOCAL}:19000/v1/turn"}
    # Act
    session = project_detail(row, ALIVE)["session"]
    # Assert
    assert session == "a" * 8


def test_detail_started_at_carried():
    # Arrange
    row = {"name": "alpha", "started_at": "T0"}
    # Act
    started = project_detail(row, ALIVE)["detail"]["started_at"]
    # Assert
    assert started == "T0"


def test_detail_workdir_not_exposed():
    # Arrange
    row = {"name": "alpha"}
    # Act
    has_workdir = "workdir" in project_detail(row, ALIVE)["detail"]
    # Assert
    assert not has_workdir


def test_detail_status_failure_is_explicit():
    # Arrange
    row = {"name": "alpha"}
    # Act
    detail = project_detail(row, Exception("endpoint down"))
    # Assert
    assert detail["detail_error"] and detail["session"] == "—"


def test_typed_spec_failure_is_not_unknown():
    # Arrange
    exc = RemoteOperationError(400, "Config validation failed for delta/spec.yaml", kind="spec_resolution_failed")
    # Act
    projected = project_row({"name": "delta"}, exc)
    # Assert
    assert projected["state_label"] == "Spec invalid"


def test_typed_ambiguous_registry_kind():
    # Arrange
    exc = RemoteOperationError(409, "two registries claim this name", kind="ambiguous_registry")
    # Act
    label = project_row({"name": "x"}, exc)["state_label"]
    # Assert
    assert label == "Ambiguous registry"


def test_plain_exception_without_kind_is_unknown():
    # Arrange
    row = {"name": "x"}
    # Act
    projected = project_row(row, Exception("boom"))
    # Assert
    assert projected["state_label"] == "Unknown" and projected["state_detail"] == ""


def test_typed_state_detail_carries_message():
    # Arrange
    exc = RemoteOperationError(400, "Config validation failed for delta/spec.yaml", kind="spec_resolution_failed")
    # Act
    detail = project_row({"name": "delta"}, exc)["state_detail"]
    # Assert
    assert "Config validation failed" in detail
