"""Tests for ``should_show_plan_and_confirm`` — the P2b launch-confirm gate.

The contract that matters: the interactive plan+confirm fires ONLY for a real
operator at a tty with no override, and is skipped for EVERY programmatic path
(so the spawn broker / supervisor / cron / scripts never block).
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg.lifecycle._start_single import (
    should_show_plan_and_confirm,
)


def _gate(**over: bool) -> bool:
    base = dict(
        yes=False,
        dry_run=False,
        as_json=False,
        foreground=False,
        one_shot=False,
        broker_self=False,
        is_tty=True,
    )
    base.update(over)
    return should_show_plan_and_confirm(**base)


def test_confirms_for_interactive_operator_at_tty() -> None:
    # Arrange — a real operator: tty, no overriding flags.
    over: dict[str, bool] = {}
    # Act
    out = _gate(**over)
    # Assert
    assert out is True


def test_skips_when_yes_flag_set() -> None:
    # Arrange — operator pre-approved with --yes.
    over = {"yes": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_on_dry_run() -> None:
    # Arrange — --dry-run already prints the plan.
    over = {"dry_run": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_on_json_output() -> None:
    # Arrange — machine-readable output, no prompts.
    over = {"as_json": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_in_foreground() -> None:
    # Arrange — foreground launch path.
    over = {"foreground": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_on_one_shot() -> None:
    # Arrange — the in-SIF one-shot runner.
    over = {"one_shot": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_on_broker_self() -> None:
    # Arrange — sac-from-sac spawn broker.
    over = {"broker_self": True}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False


def test_skips_when_not_a_tty() -> None:
    # Arrange — cron / script / supervisor (no tty) MUST never block.
    over = {"is_tty": False}
    # Act
    out = _gate(**over)
    # Assert
    assert out is False
