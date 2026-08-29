#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A spec is intent; a process holds its launch argv. Nothing compared them.

Measured 2026-08-19 on compute-04, reading ``/proc/<pid>/cmdline`` for
each running handyman:

    handyman-01   up 1d02h   NO --effort
    handyman-02   up 07h27m  --effort low
    handyman-03   up 22h     NO --effort

``--effort low`` was in TWENTY spec files and in ONE running process.
The two that lacked it had started before the edit. Every file-reading
surface reported the operator's cost instruction as applied.

These tests pin the comparison, and above all pin that "I could not
look" is a THIRD answer. Every instrument that failed us that night —
a truncated listing read as absence, a spec-vs-spec guard blind to
processes, a status probe asserting "real absence" about another host —
failed by printing its could-not-look as a clean result.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._argv_drift import (
    CANNOT_DETERMINE,
    DIFFERS,
    MATCHES,
    ArgvDrift,
    compare_spec_flags_to_argv,
    summarize,
)

_ARGV_WITH = ["claude", "--model", "qwen38-27b", "--effort", "low", "--resume", "abc"]
_ARGV_WITHOUT = ["claude", "--model", "qwen38-27b", "--resume", "abc"]


def test_an_unloadable_spec_cannot_be_decided():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=None, running_argv=_ARGV_WITH
    )
    # Assert
    assert result.verdict == CANNOT_DETERMINE


def test_an_unobserved_process_cannot_be_decided():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=None
    )
    # Assert
    assert result.verdict == CANNOT_DETERMINE


def test_an_unobserved_process_is_not_reported_as_matching():
    # Arrange: the regression — silence must not read as compliance.
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=None
    )
    # Assert
    assert result.verdict != MATCHES


def test_the_undecided_reason_names_the_missing_process():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=None
    )
    # Assert
    assert "no running process" in (result.reason or "")


def test_the_undecided_reason_names_the_unloadable_spec():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=None, running_argv=_ARGV_WITH
    )
    # Assert
    assert "spec could not be loaded" in (result.reason or "")


def test_is_drifted_is_none_when_undecided():
    # Arrange: a caller treating this as falsey turns "did not look" into "fine".
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=None
    )
    # Assert
    assert result.is_drifted is None


def test_a_process_carrying_every_declared_flag_matches():
    # Arrange
    agent = "handyman-02"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=_ARGV_WITH
    )
    # Assert
    assert result.verdict == MATCHES


def test_the_measured_handyman_incident_is_reported_as_drift():
    # Arrange: handyman-01, spec says --effort low, process predates the edit.
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=_ARGV_WITHOUT
    )
    # Assert
    assert result.verdict == DIFFERS


def test_the_drift_names_the_flag_that_is_missing():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=_ARGV_WITHOUT
    )
    # Assert
    assert result.missing == ("--effort", "low")


def test_the_drift_message_names_the_remedy():
    # Arrange
    agent = "handyman-01"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=_ARGV_WITHOUT
    )
    # Assert
    assert "restart it" in result.describe()


def test_flags_present_but_not_adjacent_are_drift():
    # Arrange: --effort separated from its value is a different command line.
    agent = "handyman-03"
    argv = ["claude", "--effort", "--model", "qwen38-27b", "low"]
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=argv
    )
    # Assert
    assert result.verdict == DIFFERS


def test_the_non_adjacent_drift_explains_itself():
    # Arrange
    agent = "handyman-03"
    argv = ["claude", "--effort", "--model", "qwen38-27b", "low"]
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=["--effort", "low"], running_argv=argv
    )
    # Assert
    assert "contiguous" in (result.reason or "")


def test_a_spec_declaring_no_flags_matches_any_process():
    # Arrange
    agent = "scitex-cards"
    # Act
    result = compare_spec_flags_to_argv(
        agent=agent, spec_flags=[], running_argv=_ARGV_WITHOUT
    )
    # Assert
    assert result.verdict == MATCHES


def test_the_summary_counts_undetermined_separately_from_matching():
    # Arrange: 1 ok, 1 drifted, 1 unknown must never render as 2 ok.
    drifts = [
        ArgvDrift(agent="a", verdict=MATCHES),
        ArgvDrift(agent="b", verdict=DIFFERS, missing=("--effort", "low")),
        ArgvDrift(agent="c", verdict=CANNOT_DETERMINE, reason="no running process"),
    ]
    # Act
    text = summarize(drifts)
    # Assert
    assert "1 in force, 1 drifted, 1 could not be determined" in text
