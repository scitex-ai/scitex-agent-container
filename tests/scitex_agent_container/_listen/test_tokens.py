"""Tests for the bearer-token storage layer."""

from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container._listen.tokens import (
    default_token_path,
    ensure_token,
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
