"""Three-valued vocabulary for the overlay-venv contract.

Mirrors ``src/scitex_agent_container/_maintenance/_overlay_venv_model.py``.

The load-bearing property is that UNKNOWN is a THIRD state, never a shade of
pass. Every test here pins one half of that: a check cannot claim a non-pass
without saying what to do about it, and a plan cannot resolve to a mutation
while any question is still open.

Read ``test_an_unknown_refuses_the_mutation_as_firmly_as_a_failure`` together
with ``test_a_safe_stale_overlay_with_a_venv_slice_invalidates``: identical
staleness, opposite actions, and the only difference is whether a question was
answered. They are separate functions only because one assertion per test is
required.

Pure dataclasses, so no filesystem and no mocks (PA-306) — there is nothing to
mock, the types take plain values.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._maintenance._overlay_venv_model import (
    ACTION_INVALIDATE,
    ACTION_NONE,
    ACTION_REFUSE,
    InvalidationPlan,
    VenvCheck,
)

PASS = VenvCheck(name="a", ok=True, detail="fine")
FAIL = VenvCheck(name="b", ok=False, detail="broken", hint="fix it")
UNKNOWN = VenvCheck(name="c", ok=None, detail="unmeasured", hint="measure it")


def test_failing_check_without_a_hint_is_rejected() -> None:
    """An error that only says what broke is half-written."""
    # Arrange
    kwargs = {"name": "x", "ok": False, "detail": "broken"}
    # Act
    act = lambda: VenvCheck(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="hint"):
        act()


def test_unknown_check_without_a_hint_is_rejected() -> None:
    """UNKNOWN must prescribe 'go measure it', not merely report silence."""
    # Arrange
    kwargs = {"name": "x", "ok": None, "detail": "unmeasured"}
    # Act
    act = lambda: VenvCheck(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="hint"):
        act()


def test_passing_check_needs_no_hint() -> None:
    # Arrange
    name = "x"
    # Act
    check = VenvCheck(name=name, ok=True, detail="fine")
    # Assert
    assert check.hint == ""


def test_check_rejects_a_non_three_valued_ok() -> None:
    # Arrange
    kwargs = {"name": "x", "ok": "yes", "detail": "fine"}
    # Act
    act = lambda: VenvCheck(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="True/False/None"):
        act()


def test_plan_with_only_passes_is_safe() -> None:
    # Arrange
    checks = (PASS,)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert plan.safe is True


def test_plan_with_a_failure_is_unsafe() -> None:
    # Arrange
    checks = (PASS, FAIL)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert plan.safe is False


def test_plan_with_an_unknown_is_neither_safe_nor_unsafe() -> None:
    """The whole point: 'could not tell' is not 'it was fine'."""
    # Arrange
    checks = (PASS, UNKNOWN)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert plan.safe is None


def test_unknowns_are_reported_separately_from_failures() -> None:
    """A reader must be able to tell 'this is wrong' from 'I could not tell'."""
    # Arrange
    checks = (FAIL, UNKNOWN)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert (plan.failed, plan.unknown) == ((FAIL,), (UNKNOWN,))


def test_an_unknown_refuses_the_mutation_as_firmly_as_a_failure() -> None:
    """Folding 'could not read' into 'fine' is the bug class this rail closes."""
    # Arrange
    checks = (UNKNOWN,)
    # Act
    plan = InvalidationPlan(
        agent="a",
        overlay_root="/o",
        checks=checks,
        stale=True,
        venv_slice_present=True,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_failure_refuses_the_mutation() -> None:
    # Arrange
    checks = (FAIL,)
    # Act
    plan = InvalidationPlan(
        agent="a",
        overlay_root="/o",
        checks=checks,
        stale=True,
        venv_slice_present=True,
    )
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_safe_stale_overlay_with_a_venv_slice_invalidates() -> None:
    # Arrange
    checks = (PASS,)
    # Act
    plan = InvalidationPlan(
        agent="a",
        overlay_root="/o",
        checks=checks,
        stale=True,
        venv_slice_present=True,
    )
    # Assert
    assert plan.action == ACTION_INVALIDATE


def test_a_safe_stale_overlay_with_no_venv_slice_does_nothing() -> None:
    """Nothing to shadow with, so nothing to move — but still not a refusal."""
    # Arrange
    checks = (PASS,)
    # Act
    plan = InvalidationPlan(
        agent="a",
        overlay_root="/o",
        checks=checks,
        stale=True,
        venv_slice_present=False,
    )
    # Assert
    assert plan.action == ACTION_NONE


def test_a_safe_fresh_overlay_does_nothing() -> None:
    # Arrange
    checks = (PASS,)
    # Act
    plan = InvalidationPlan(
        agent="a",
        overlay_root="/o",
        checks=checks,
        stale=False,
        venv_slice_present=True,
    )
    # Assert
    assert plan.action == ACTION_NONE


def test_blocking_reasons_carry_the_hint_not_only_the_complaint() -> None:
    # Arrange
    checks = (FAIL,)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert plan.blocking_reasons() == ("b: broken -> fix it",)


def test_blocking_reasons_are_empty_on_a_clean_plan() -> None:
    # Arrange
    checks = (PASS,)
    # Act
    plan = InvalidationPlan(agent="a", overlay_root="/o", checks=checks)
    # Assert
    assert plan.blocking_reasons() == ()


def test_plan_requires_an_agent_name() -> None:
    """An anonymous refusal is unactionable — the log line must name someone."""
    # Arrange
    kwargs = {"agent": "", "overlay_root": "/o"}
    # Act
    act = lambda: InvalidationPlan(**kwargs)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="agent"):
        act()
