"""Tests for ``_django/_resources`` (honest process-resource observation).

Pure-function coverage — no mocks, no monkeypatch. Each test has AAA markers
and a single assertion (STX-TQ002/TQ007). The key invariant: an unreadable pid
is never reported as "ok" with invented numbers.
"""

from __future__ import annotations

import os

from scitex_agent_container._django._resources import read_resources


def test_no_pid_is_unavailable():
    # Arrange
    out = read_resources(None)
    # Act
    state = out["state"]
    # Assert
    assert state == "unavailable" and out["pid"] is None


def test_bad_pid_is_unavailable():
    # Arrange
    out = read_resources("not-a-pid")
    # Act
    state = out["state"]
    # Assert
    assert state == "unavailable"


def test_negative_pid_is_unavailable():
    # Arrange
    out = read_resources(-1)
    # Act
    state = out["state"]
    # Assert
    assert state == "unavailable"


def test_unknown_pid_never_fabricated():
    # Arrange
    out = read_resources(499999999)
    # Act
    state = out["state"]
    # Assert
    assert state in {"namespace", "unavailable"}


def test_own_pid_is_ok_with_real_rss():
    # Arrange
    out = read_resources(os.getpid())
    # Act
    ok = out["state"] == "ok" and isinstance(out["rss"], str) and out["rss"]
    # Assert
    assert ok


def test_own_pid_reports_tree_size():
    # Arrange
    out = read_resources(os.getpid())
    # Act
    tree = out.get("tree_size")
    # Assert
    assert isinstance(tree, int) and tree >= 1
