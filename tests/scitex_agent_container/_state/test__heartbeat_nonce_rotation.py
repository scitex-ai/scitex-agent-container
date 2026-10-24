"""Rotation-policy tests for the per-heartbeat challenge nonce (CCT 4309).

NO MONKEYPATCH (PA-306 §3): env is saved and restored directly.
"""

from __future__ import annotations

import contextlib
import os

from scitex_agent_container._state.authoritative_heartbeat import (
    maybe_rotate_challenge_nonce,
    plan_challenge_rotation,
    read_challenge_nonce,
)


@contextlib.contextmanager
def _rotation_env(value):
    """Pin SAC_NONCE_CHALLENGE_ROTATION for one test, then restore."""
    saved = os.environ.get("SAC_NONCE_CHALLENGE_ROTATION")
    if value is None:
        os.environ.pop("SAC_NONCE_CHALLENGE_ROTATION", None)
    else:
        os.environ["SAC_NONCE_CHALLENGE_ROTATION"] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_NONCE_CHALLENGE_ROTATION", None)
        else:
            os.environ["SAC_NONCE_CHALLENGE_ROTATION"] = saved


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


def test_rotation_disabled_by_default_writes_nothing(tmp_path) -> None:
    # Arrange
    # Act
    with _rotation_env(None):
        maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Assert
    assert not (tmp_path / "nonce-challenge.txt").exists()


def test_rotation_enabled_enrolls_fresh_challenge(tmp_path) -> None:
    # Arrange
    # Act
    with _rotation_env("1"):
        active = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Assert
    assert read_challenge_nonce(tmp_path) == active


def test_rotation_enabled_rotates_after_confirmed_echo(tmp_path) -> None:
    # Arrange
    with _rotation_env("1"):
        first = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
        old = tmp_path / "nonce-challenge.txt"
        os.utime(old, (100.0, 100.0))
    # Act
    with _rotation_env("1"):
        second = maybe_rotate_challenge_nonce(
            tmp_path, first, first, now=1000.0 + 301.0
        )
    # Assert
    assert second != first


def test_rotation_enabled_keeps_without_echo(tmp_path) -> None:
    # Arrange
    with _rotation_env("1"):
        first = maybe_rotate_challenge_nonce(tmp_path, None, None, now=1000.0)
    # Act
    with _rotation_env("1"):
        active = maybe_rotate_challenge_nonce(
            tmp_path, first, None, now=1000.0 + 301.0
        )
    # Assert
    assert active == first
