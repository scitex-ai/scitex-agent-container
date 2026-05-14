"""Tests for cli_pkg.lifecycle._cleanup (sac registry clean)."""

from __future__ import annotations

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._cleanup as _c
from scitex_agent_container.cli_pkg.lifecycle._cleanup import cleanup


class _FakeRegistry:
    def __init__(self, stale_count: int = 0):
        self._stale = stale_count

    def cleanup_stale(self):
        return self._stale


def test_cleanup_dry_run_prints_and_exits(monkeypatch):
    called = {"clean": False}

    class _R:
        def cleanup_stale(self):
            called["clean"] = True
            return 0

    monkeypatch.setattr(_c, "Registry", _R)
    runner = CliRunner()
    result = runner.invoke(cleanup, ["--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    # cleanup_stale must NOT be called on dry-run.
    assert called["clean"] is False


def test_cleanup_without_yes_refuses(monkeypatch):
    monkeypatch.setattr(_c, "Registry", lambda: _FakeRegistry(0))
    runner = CliRunner()
    result = runner.invoke(cleanup, [])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_cleanup_with_yes_no_stale(monkeypatch):
    monkeypatch.setattr(_c, "Registry", lambda: _FakeRegistry(0))
    runner = CliRunner()
    result = runner.invoke(cleanup, ["--yes"])
    assert result.exit_code == 0
    assert "No stale" in result.output


def test_cleanup_with_yes_some_stale(monkeypatch):
    monkeypatch.setattr(_c, "Registry", lambda: _FakeRegistry(3))
    runner = CliRunner()
    result = runner.invoke(cleanup, ["-y"])
    assert result.exit_code == 0
    assert "Cleaned 3" in result.output
