"""Tests for cli_pkg.lifecycle._cleanup.

PA-306: no ``unittest.mock`` and no ``monkeypatch``. The production
collaborator ``Registry`` is swapped at the module namespace via a
small context manager with explicit save/restore.

TQ cleanup: module docstring summarises intent (TQ001), every test
carries AAA markers (TQ002), test names spell out the behaviour being
verified (TQ003-compatible), and each test asserts exactly one fact
(TQ007). Same-shape invariants collapse into ``pytest.parametrize``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle import _cleanup as _c
from scitex_agent_container.cli_pkg.lifecycle._cleanup import cleanup

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, stale_count: int = 0):
        self._stale = stale_count
        self.cleanup_calls = 0

    def cleanup_stale(self) -> int:
        self.cleanup_calls += 1
        return self._stale


@contextmanager
def _swap_registry(factory: Any) -> Iterator[None]:
    saved = _c.Registry
    _c.Registry = factory  # type: ignore[assignment]
    try:
        yield
    finally:
        _c.Registry = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Dry-run path
# ---------------------------------------------------------------------------


def test_dry_run_exits_with_zero_status_code():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, ["--dry-run"])
    # Assert
    assert result.exit_code == 0, result.output


def test_dry_run_announces_dry_run_in_output():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, ["--dry-run"])
    # Assert
    assert "dry-run" in result.output


def test_dry_run_does_not_invoke_cleanup_stale():
    # Arrange
    reg = _FakeRegistry(5)
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: reg):
        runner.invoke(cleanup, ["--dry-run"])
    # Assert
    assert reg.cleanup_calls == 0


# ---------------------------------------------------------------------------
# Refuse path (no --yes flag)
# ---------------------------------------------------------------------------


def test_without_yes_exits_with_status_two():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, [])
    # Assert
    assert result.exit_code == 2


def test_without_yes_reports_refusal_message():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, [])
    # Assert
    assert "Refusing" in result.output


def test_without_yes_does_not_invoke_cleanup_stale():
    # Arrange
    reg = _FakeRegistry(5)
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: reg):
        runner.invoke(cleanup, [])
    # Assert
    assert reg.cleanup_calls == 0


# ---------------------------------------------------------------------------
# Confirmed path — no stale entries
# ---------------------------------------------------------------------------


def test_yes_with_no_stale_exits_with_zero_status_code():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, ["--yes"])
    # Assert
    assert result.exit_code == 0, result.output


def test_yes_with_no_stale_emits_no_stale_marker_in_output():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(0)):
        result = runner.invoke(cleanup, ["--yes"])
    # Assert
    assert "No stale" in result.output


# ---------------------------------------------------------------------------
# Confirmed path — some stale entries
# ---------------------------------------------------------------------------


def test_yes_with_stale_exits_with_zero_status_code():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(3)):
        result = runner.invoke(cleanup, ["-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_yes_with_stale_reports_cleaned_count_in_output():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: _FakeRegistry(3)):
        result = runner.invoke(cleanup, ["-y"])
    # Assert
    assert "Cleaned 3" in result.output


def test_yes_with_stale_invokes_cleanup_stale_once():
    # Arrange
    reg = _FakeRegistry(3)
    runner = CliRunner()
    # Act
    with _swap_registry(lambda: reg):
        runner.invoke(cleanup, ["-y"])
    # Assert
    assert reg.cleanup_calls == 1
