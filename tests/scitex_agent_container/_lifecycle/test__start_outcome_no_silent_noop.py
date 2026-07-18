#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A no-op start must not masquerade as a real one (incident 2026-07-12).

Real evidence this pins, from ``scitex-storage``'s runtime dir
(``STARTUP_FAILED``, ``phase=post_ack_liveness``,
``kind=post_ack_no_apptainer_pid``, ``exit_code=0``) — the child's own
stderr, verbatim and in this order::

    Agent 'scitex-storage' is already running. No-op. Use --force to restart.
    SUCC: scitex-storage started (ywata-note-win@.../scitex-storage:/work)

    [listen post-ack liveness probe] post_ack_no_apptainer_pid: `sac agents
    start` returned rc=0 but no apptainer_pid file appeared ... within 5.0s.

Two lies in four lines: the start leg announced SUCC over an agent it had
just declined to touch, and because nothing was launched no
``apptainer_pid`` was ever written. The API answered
``{"restarted": true, "dispatched": false}`` with rc=0 over a process
whose pid never changed.

Root cause: ``agent_start`` returned a bare ``True`` from the
already-running no-op branch — byte-identical to the ``True`` a real
launch returns — so NO caller could tell the two apart. These tests pin
the tagged-but-truthy contract that restores that distinction.
"""

from __future__ import annotations

import inspect

from scitex_agent_container._lifecycle._start_outcome import (
    KIND_ALREADY_RUNNING,
    NOOP_ALREADY_RUNNING,
    StartOutcome,
    outcome_kind,
)


class TestNoOpIsTruthyForBackwardCompatibility:
    """Truthiness is load-bearing: an idempotent start IS a success."""

    def test_no_op_is_truthy_so_idempotent_start_still_succeeds(self):
        # Arrange: the sentinel returned by the already-running branch.
        outcome = NOOP_ALREADY_RUNNING
        # Act
        truthy = bool(outcome)
        # Assert: downgrading this to False would invent the mirror-image
        # lie of the bug being fixed (a false FAILURE).
        assert truthy is True

    def test_no_op_survives_the_deliberate_is_not_False_check(self):
        # Arrange: cli_pkg/lifecycle/_restart.py uses `is not False` (not
        # bool()) so a runtime returning None is not misread as failure.
        outcome = NOOP_ALREADY_RUNNING
        # Act
        passes = outcome is not False
        # Assert
        assert passes is True

    def test_no_op_compares_equal_to_True_for_equality_callers(self):
        # Arrange
        outcome = NOOP_ALREADY_RUNNING
        # Act
        equal = outcome == True  # noqa: E712
        # Assert
        assert equal is True

    def test_no_op_is_NOT_identical_to_the_True_singleton(self):
        """The ONE form that does not survive — pinned, not hidden.

        ``bool`` cannot be subclassed, so no object can ever BE the ``True``
        singleton; ``result is True`` is therefore the single caller idiom
        this change breaks. CI caught exactly two call sites
        (``test_lifecycle.py::test_agent_start_idempotent_when_already_running``
        and ``test__start_verdict.py::test_an_alive_agent_no_op_returns_success``);
        both asserted SUCCESS, not object identity, so both were rewritten to
        ``bool(...)`` plus an ``outcome_kind`` check — strictly stronger than
        what they asserted before. No production code uses the identity form
        on a start result.

        This test states that limitation as a fact so the next reader meets
        it here rather than in a red pipeline.
        """
        # Arrange
        outcome = NOOP_ALREADY_RUNNING
        # Act
        identical = outcome is True
        # Assert
        assert identical is False


class TestNoOpNamesItself:
    """The information the bare ``True`` destroyed is now recoverable."""

    def test_no_op_carries_the_already_running_kind(self):
        # Arrange
        outcome = NOOP_ALREADY_RUNNING
        # Act
        kind = outcome_kind(outcome)
        # Assert: a bare `True` cannot answer this, which is exactly why
        # the false restart was unfalsifiable from the outside.
        assert kind == KIND_ALREADY_RUNNING

    def test_a_real_start_is_distinguishable_from_the_no_op(self):
        # Arrange: both results are truthy; only the kind separates them.
        real_start = True
        # Act
        same_kind = outcome_kind(real_start) == outcome_kind(NOOP_ALREADY_RUNNING)
        # Assert
        assert same_kind is False

    def test_outcome_is_constructible_with_an_arbitrary_kind(self):
        # Arrange
        other = StartOutcome(1, "brokered-to-host")
        # Act
        kind = outcome_kind(other)
        # Assert
        assert kind == "brokered-to-host"


class TestOutcomeKindIsSafeOnLegacyResults:
    """Older paths and hand-rolled doubles still return plain booleans."""

    def test_outcome_kind_of_plain_true_is_none(self):
        # Arrange
        legacy = True
        # Act
        kind = outcome_kind(legacy)
        # Assert
        assert kind is None

    def test_outcome_kind_of_plain_false_is_none(self):
        # Arrange
        legacy = False
        # Act
        kind = outcome_kind(legacy)
        # Assert
        assert kind is None

    def test_outcome_kind_of_none_is_none(self):
        # Arrange: a runtime that returns None must not explode here.
        legacy = None
        # Act
        kind = outcome_kind(legacy)
        # Assert
        assert kind is None


class TestStartReturnsTheSentinelOnTheNoOpBranch:
    """The production no-op branch returns the tagged outcome."""

    def test_start_no_op_branch_returns_sentinel_not_bare_true(self):
        # Arrange: read the real production source, not a copy of it.
        from scitex_agent_container._lifecycle import _start

        source = inspect.getsource(_start.agent_start)
        # Act
        returns_sentinel = "return NOOP_ALREADY_RUNNING" in source
        # Assert: a future revert to a bare `return True` must turn this
        # RED — the failure mode it re-opens is invisible by construction.
        assert returns_sentinel is True, (
            "the already-running no-op branch must return the TAGGED "
            "outcome; a bare `return True` is indistinguishable from a "
            "real launch and is what let a restart report success over "
            "an agent that never cycled (incident 2026-07-12)"
        )
