"""Contract tests for privacy-safe host-authoritative heartbeat leases."""

from __future__ import annotations

import pytest

from scitex_agent_container._state.authoritative_heartbeat import (
    AuthoritativeHeartbeatError,
    classify_resident_state,
    issue_challenge_nonce,
    nonce_echo_confirms,
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


def test_stable_identity_cannot_change_inside_one_boot() -> None:
    # Arrange
    changed = _beat(model="other-model", seq=8, monotonic_ns=800, observed_at=102.0)
    # Act
    try:
        validate_heartbeat(
            changed,
            expected_agent="scholar",
            expected_host="compute-04",
            now=102.0,
            previous=_beat(),
        )
    except AuthoritativeHeartbeatError as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert "stable identity changed" in error


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Positive session delta in window → WORKING.
        (_beat(session_jsonl_delta_bytes=512), "working"),
        # Positive subagent/tasks delta alone → WORKING.
        (_beat(subagent_jsonl_delta_bytes=2048), "working"),
        # Zero deltas = no work evidence → DEAD (never idle/ready).
        (_beat(session_jsonl_delta_bytes=0, subagent_jsonl_delta_bytes=0), "dead"),
        # Idle beat (no delta keys at all) → DEAD.
        (_beat(), "dead"),
        # Negative deltas clamp to zero → DEAD.
        (
            _beat(session_jsonl_delta_bytes=-10, subagent_jsonl_delta_bytes=-5),
            "dead",
        ),
        # Non-numeric deltas read as zero → DEAD.
        (
            _beat(session_jsonl_delta_bytes="lots", subagent_jsonl_delta_bytes=None),
            "dead",
        ),
    ],
)
def test_resident_state_classification(payload, expected) -> None:
    # Arrange — the default _beat() fixture carries state="active" with
    # progress_seq=4; the verdict must ignore counters/phase entirely.
    # Act
    state = classify_resident_state(payload, now=101.0)
    # Assert
    assert state == expected


def test_resident_state_window_sums_deltas() -> None:
    # Arrange — beats fire ~60s; the 10-minute window sums deltas: one
    # zero-delta latest with a positive older in-window beat is WORKING.
    # Act
    state = classify_resident_state(
        [
            _beat(observed_at=90.0, session_jsonl_delta_bytes=300),
            _beat(
                observed_at=100.0,
                seq=8,
                monotonic_ns=800,
                session_jsonl_delta_bytes=0,
            ),
        ],
        now=101.0,
    )
    # Assert
    assert state == "working"


def test_resident_state_out_of_window_deltas_are_dead() -> None:
    # Arrange — positive deltas older than the window do not count.
    # Act
    state = classify_resident_state(
        _beat(observed_at=90.0, session_jsonl_delta_bytes=9000), now=101.0, window_s=5.0
    )
    # Assert
    assert state == "dead"


def test_nonce_match_plus_delta_is_working() -> None:
    # Arrange — dual confirmation (CCT 4309): challenge + byte-exact echo.
    # Act
    state = classify_resident_state(
        _beat(
            session_jsonl_delta_bytes=128,
            nonce_challenge="1234567890123456",
            nonce_echo="1234567890123456",
        ),
        now=101.0,
    )
    # Assert
    assert state == "working"


def test_nonce_mismatch_is_dead_despite_positive_delta() -> None:
    # Arrange — failed echo confirmation kills even with byte evidence.
    # Act
    state = classify_resident_state(
        _beat(
            session_jsonl_delta_bytes=128,
            nonce_challenge="1234567890123456",
            nonce_echo="9999999999999999",
        ),
        now=101.0,
    )
    # Assert
    assert state == "dead"


def test_nonce_missing_echo_is_dead_despite_positive_delta() -> None:
    # Arrange — challenge with no reflected echo = failed confirmation.
    # Act
    state = classify_resident_state(
        _beat(
            session_jsonl_delta_bytes=128,
            nonce_challenge="1234567890123456",
        ),
        now=101.0,
    )
    # Assert
    assert state == "dead"


def test_nonce_protocol_helpers() -> None:
    # Arrange
    # Act
    challenge_a = issue_challenge_nonce()
    challenge_b = issue_challenge_nonce()
    # Assert — 16-digit zero-padded tokens; confirm rules.
    assert len(challenge_a) == 16 and challenge_a.isdigit()
    assert len(challenge_b) == 16 and challenge_b.isdigit()
    assert (
        nonce_echo_confirms(
            {"nonce_challenge": challenge_a, "nonce_echo": challenge_a}
        )
        is True
    )
    assert (
        nonce_echo_confirms({"nonce_challenge": challenge_a, "nonce_echo": "0" * 16})
        is False
    )
    assert nonce_echo_confirms({}) is None


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
