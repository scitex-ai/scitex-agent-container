"""Tests for cli_pkg.lifecycle._cleanup.

PA-306: no ``monkeypatch``. Hand-rolled ``_swap_registry`` context
manager save/restores ``_c.Registry`` for each test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle import _cleanup as _c
from scitex_agent_container.cli_pkg.lifecycle._cleanup import cleanup


class _FakeRegistry:
    def __init__(self, stale_count: int = 0):
        self._stale = stale_count

    def cleanup_stale(self):
        return self._stale


@contextmanager
def _swap_registry(factory: Any) -> Iterator[None]:
    saved = _c.Registry
    _c.Registry = factory  # type: ignore[assignment]
    try:
        yield
    finally:
        _c.Registry = saved  # type: ignore[assignment]


def test_cleanup_dry_run_prints_and_exits():
    called = {"clean": False}

    class _R:
        def cleanup_stale(self):
            called["clean"] = True
            return 0

    with _swap_registry(_R):
        runner = CliRunner()
        result = runner.invoke(cleanup, ["--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    # cleanup_stale must NOT be called on dry-run.
    assert called["clean"] is False


def test_cleanup_without_yes_refuses():
    with _swap_registry(lambda: _FakeRegistry(0)):
        runner = CliRunner()
        result = runner.invoke(cleanup, [])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_cleanup_with_yes_no_stale():
    with _swap_registry(lambda: _FakeRegistry(0)):
        runner = CliRunner()
        result = runner.invoke(cleanup, ["--yes"])
    assert result.exit_code == 0
    assert "No stale" in result.output


def test_cleanup_with_yes_some_stale():
    with _swap_registry(lambda: _FakeRegistry(3)):
        runner = CliRunner()
        result = runner.invoke(cleanup, ["-y"])
    assert result.exit_code == 0
    assert "Cleaned 3" in result.output
