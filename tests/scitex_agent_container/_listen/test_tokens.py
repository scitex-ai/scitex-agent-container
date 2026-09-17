"""Tests for the bearer-token storage layer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen.tokens import (
    default_owner_token_path,
    default_token_path,
    ensure_owner_token,
    ensure_token,
    read_owner_token,
    read_token,
)


def test_default_token_path_layout(tmp_path: Path):
    # Arrange
    home = tmp_path
    # Act
    p = default_token_path(home=home, hostname="alpha")
    # Assert
    assert (
        p == tmp_path / ".scitex" / "agent-container" / "tokens" / "listen-alpha.token"
    )


def test_ensure_token_creates_file(tmp_path: Path):
    # Arrange
    p = tmp_path / "t.token"
    # Act
    ensure_token(p)
    # Assert
    assert p.is_file()


def test_ensure_token_returns_token_of_sufficient_length(tmp_path: Path):
    # Arrange
    p = tmp_path / "t.token"
    # Act
    t1 = ensure_token(p)
    # Assert
    assert len(t1) >= 32


def test_ensure_token_sets_mode_0600(tmp_path: Path):
    # Arrange
    p = tmp_path / "t.token"
    # Act
    ensure_token(p)
    # Assert
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_ensure_token_is_idempotent(tmp_path: Path):
    # Arrange
    p = tmp_path / "t.token"
    t1 = ensure_token(p)
    # Act
    t2 = ensure_token(p)
    # Assert
    assert t1 == t2


def test_read_token_missing_returns_none(tmp_path: Path):
    # Arrange
    missing = tmp_path / "absent"
    # Act
    result = read_token(missing)
    # Assert
    assert result is None


def test_read_token_strips_whitespace(tmp_path: Path):
    # Arrange
    p = tmp_path / "t"
    p.write_text("  abc\n", encoding="utf-8")
    # Act
    result = read_token(p)
    # Assert
    assert result == "abc"


def test_owner_token_uses_non_home_runtime_path(tmp_path: Path):
    # Arrange
    expected = tmp_path / "scitex-agent-container" / "fork-owner-host-a.token"
    # Act
    path = default_owner_token_path(runtime_dir=tmp_path, hostname="host-a")
    # Assert
    assert path == expected


def test_owner_token_is_exact_0600_and_round_trips(tmp_path: Path):
    # Arrange
    path = tmp_path / "private" / "fork-owner.token"

    # Act
    created = ensure_owner_token(path)

    # Assert
    assert (read_owner_token(path), path.stat().st_mode & 0o777) == (created, 0o600)


def test_owner_token_reader_refuses_symlink(tmp_path: Path):
    # Arrange
    source = tmp_path / "source"
    source.write_text("not-secret", encoding="utf-8")
    source.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(source)

    # Act
    action = pytest.raises(OSError)
    # Assert
    with action:
        read_owner_token(alias)
