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
    p = default_token_path(home=tmp_path, hostname="alpha")
    assert (
        p == tmp_path / ".scitex" / "agent-container" / "tokens" / "listen-alpha.token"
    )


def test_ensure_token_creates_and_is_idempotent(tmp_path: Path):
    p = tmp_path / "t.token"
    t1 = ensure_token(p)
    assert p.is_file()
    assert len(t1) >= 32
    # Mode 0600
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    # Re-call returns the same token (idempotent)
    t2 = ensure_token(p)
    assert t1 == t2


def test_read_token_missing_returns_none(tmp_path: Path):
    assert read_token(tmp_path / "absent") is None


def test_read_token_strips_whitespace(tmp_path: Path):
    p = tmp_path / "t"
    p.write_text("  abc\n", encoding="utf-8")
    assert read_token(p) == "abc"
