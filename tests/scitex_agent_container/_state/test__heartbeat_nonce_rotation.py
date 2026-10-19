"""Rotation-policy tests for the per-heartbeat challenge nonce (CCT 4309)."""

from __future__ import annotations

import os

from scitex_agent_container._state.authoritative_heartbeat import (
    maybe_rotate_challenge_nonce,
    plan_challenge_rotation,
    read_challenge_nonce,
)


def test_no_enrolled_challenge_plans_enroll() -> None:
    # Arrange
    # Act
    action = plan_challenge_rotation(
        enrolled=None, echo=None, now=1000.0, last_rotated_at=None
    )
    # Assert
    assert action == "enroll"


def test_confirmed_echo_after_interval_plans_rotate() -> None:
    # Arrange
    # Act
    action = plan_challenge_rotation(
        enrolled="1111111111111111",
        echo="1111111111111111",
        now=1000.0,
        last_rotated_at=100.0,
    )
    # Assert
    assert action == "rotate"


def test_confirmed_echo_within_interval_keeps() -> None:
    # Arrange
    # Act
    action = plan_challenge_rotation(
        enrolled="1111111111111111",
        echo="1111111111111111",
        now=1000.0,
        last_rotated_at=990.0,
    )
    # Assert
    assert action == "keep"


def test_mismatched_echo_keeps() -> None:
    # Arrange
    # Act
    action = plan_challenge_rotation(
        enrolled="1111111111111111",
        echo="2222222222222222",
        now=1000.0,
        last_rotated_at=100.0,
    )
    # Assert
    assert action == "keep"


def test_missing_echo_keeps() -> None:
    # Arrange
    # Act
    action = plan_challenge_rotation(
        enrolled="1111111111111111",
        echo=None,
        now=1000.0,
        last_rotated_at=100.0,
    )
    # Assert
    assert action == "keep"


def test_rotation_disabled_by_default_writes_nothing(
    tmp_path, monkeypatch
) -> None:
    # Arrange
    monkeypatch.delenv("SAC_NONCE_CHALLENGE_ROTATION", raising=False)
    # Act
    maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Assert
    assert not (tmp_path / "nonce-challenge.txt").exists()


def test_rotation_enabled_enrolls_fresh_challenge(
    tmp_path, monkeypatch
) -> None:
    # Arrange
    monkeypatch.setenv("SAC_NONCE_CHALLENGE_ROTATION", "1")
    # Act
    active = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Assert
    assert read_challenge_nonce(tmp_path) == active


def test_rotation_enabled_rotates_after_confirmed_echo(
    tmp_path, monkeypatch
) -> None:
    # Arrange
    monkeypatch.setenv("SAC_NONCE_CHALLENGE_ROTATION", "1")
    first = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    old = tmp_path / "nonce-challenge.txt"
    os.utime(old, (100.0, 100.0))
    # Act
    second = maybe_rotate_challenge_nonce(
        tmp_path, first, first, now=1000.0 + 301.0
    )
    # Assert
    assert second != first


def test_rotation_enabled_keeps_without_echo(tmp_path, monkeypatch) -> None:
    # Arrange
    monkeypatch.setenv("SAC_NONCE_CHALLENGE_ROTATION", "1")
    first = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Act
    active = maybe_rotate_challenge_nonce(
        tmp_path, first, None, now=1000.0 + 301.0
    )
    # Assert
    assert active == first
