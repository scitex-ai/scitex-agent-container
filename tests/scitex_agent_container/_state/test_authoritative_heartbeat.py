"""Contract tests for privacy-safe host-authoritative heartbeat leases."""

from __future__ import annotations

import pytest

from scitex_agent_container._state.authoritative_heartbeat import (
    AuthoritativeHeartbeatError,
    classify_resident_state,
    read_card_lease,
    select_federated_heartbeats,
    validate_heartbeat,
    write_card_lease,
)


def _beat(**overrides):
    payload = {
        "agent_id": "scholar",
        "spec_id": "sha256:spec-123",
        "host": "compute-04",
        "runtime": "tui",
        "harness": "hermes",
        "engine": "vllm",
        "model": "qwen3-coder",
        "session_id": "stored-session-1",
        "boot_id": "boot-1",
        "seq": 7,
        "monotonic_ns": 700,
        "observed_at": 100.0,
        "progress_at": 99.0,
        "progress_seq": 4,
        "state": "active",
        "lease_expires_at": 130.0,
        "card_id": "card-1",
        "card_role": "developer",
    }
    payload.update(overrides)
    return payload


def test_valid_heartbeat_keeps_only_privacy_safe_identity_and_progress() -> None:
    # Arrange
    # Act
    heartbeat = validate_heartbeat(
        _beat(), expected_agent="scholar", expected_host="compute-04", now=101.0
    )
    # Assert
    assert heartbeat == _beat()


def test_prompt_or_tool_payload_is_rejected() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(AuthoritativeHeartbeatError, match="unsupported field"):
        validate_heartbeat(
            _beat(prompt="secret prompt"),
            expected_agent="scholar",
            expected_host="compute-04",
            now=101.0,
        )


def test_spoofed_agent_or_host_is_rejected() -> None:
    # Arrange
    # Act
    errors = []
    for payload in (_beat(agent_id="victim"), _beat(host="compute-03")):
        try:
            validate_heartbeat(
                payload,
                expected_agent="scholar",
                expected_host="compute-04",
                now=101.0,
            )
        except AuthoritativeHeartbeatError as exc:
            errors.append(str(exc))
    # Assert
    assert tuple("identity mismatch" in error for error in errors) == (True, True)


def test_duplicate_out_of_order_and_future_beats_are_rejected() -> None:
    # Arrange
    previous = _beat()
    payloads = (
        _beat(),
        _beat(seq=6, monotonic_ns=701, observed_at=102.0),
        _beat(seq=8, monotonic_ns=699, observed_at=102.0),
        _beat(seq=8, monotonic_ns=800, observed_at=120.0, lease_expires_at=150.0),
    )
    # Act
    refused = []
    for payload in payloads:
        try:
            validate_heartbeat(
                payload,
                expected_agent="scholar",
                expected_host="compute-04",
                now=101.0,
                previous=previous,
            )
        except AuthoritativeHeartbeatError as exc:
            refused.append(str(exc))
    # Assert
    assert (
        len(refused),
        any("sequence" in value for value in refused),
        any("monotonic" in value for value in refused),
        any("future" in value for value in refused),
    ) == (4, True, True, True)


def test_restart_requires_new_boot_and_resets_sequence() -> None:
    # Arrange
    # Act
    restarted = validate_heartbeat(
        _beat(
            boot_id="boot-2",
            seq=1,
            monotonic_ns=10,
            observed_at=102.0,
            progress_at=102.0,
            progress_seq=0,
            lease_expires_at=132.0,
        ),
        expected_agent="scholar",
        expected_host="compute-04",
        now=102.0,
        previous=_beat(),
    )
    # Assert
    assert (restarted["boot_id"], restarted["seq"]) == ("boot-2", 1)


@pytest.mark.parametrize(
    ("payload", "process_alive", "connected", "expected"),
    [
        (_beat(state="idle"), True, True, "idle"),
        (_beat(state="active"), True, True, "active"),
        (_beat(state="blocked"), True, True, "blocked"),
        (_beat(state="active", progress_at=50.0), True, True, "stalled"),
        (_beat(lease_expires_at=90.0), True, False, "disconnected"),
        (_beat(lease_expires_at=90.0), False, False, "dead"),
    ],
)
def test_resident_state_classification(
    payload, process_alive, connected, expected
) -> None:
    # Arrange
    # Act
    state = classify_resident_state(
        payload,
        now=101.0,
        process_alive=process_alive,
        federation_connected=connected,
        progress_stale_s=30.0,
    )
    # Assert
    assert state == expected


def test_card_lease_role_is_bounded() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(AuthoritativeHeartbeatError, match="card_role"):
        validate_heartbeat(
            _beat(card_role="owner"),
            expected_agent="scholar",
            expected_host="compute-04",
            now=101.0,
        )


def test_card_developer_or_reviewer_lease_expires_fail_closed(tmp_path) -> None:
    # Arrange
    # Act
    write_card_lease(
        tmp_path,
        card_id="card-1",
        role="reviewer",
        expires_at=200.0,
        now=100.0,
    )
    current = read_card_lease(tmp_path, now=199.0)
    expired = read_card_lease(tmp_path, now=201.0)
    # Assert
    assert (current, expired) == (("card-1", "reviewer"), ("", ""))


def test_failover_waits_for_old_host_lease_to_expire() -> None:
    # Arrange
    old = _beat(host="compute-03", boot_id="old", observed_at=90.0, lease_expires_at=110.0)
    new = _beat(host="compute-04", boot_id="new", observed_at=101.0, lease_expires_at=131.0)
    # Act
    try:
        select_federated_heartbeats([old, new], now=105.0)
    except AuthoritativeHeartbeatError as exc:
        error = str(exc)
    else:
        error = ""
    selected = select_federated_heartbeats([old, new], now=111.0)
    # Assert
    assert (
        "overlapping" in error,
        selected[0]["host"],
        selected[0]["boot_id"],
    ) == (True, "compute-04", "new")
