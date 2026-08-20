#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A refusal must name its reason; 5xx is for genuine server faults.

``POST /agents`` answered 502 for EVERY non-zero child exit. hub hit it
on 2026-08-19 standing up a scholar agent, and stated the defect better
than the code did:

    a 500 cannot distinguish "your request was wrong" from "the server
    is broken", and those need OPPOSITE responses from the caller.

They waited for the daemon owner and reported a permissions bug that did
not exist. The response BODY carried the real reason throughout — an
expired-credential message that was true about a file and silent about
the request. The status code was what threw the distinction away.

These tests pin that unambiguous, caller-fixable failures get a 4xx that
says which, and that everything else still gets 502. The default matters
as much as the classification: a server fault misreported as 4xx sends
someone to fix a call that was already correct.
"""

from __future__ import annotations

from scitex_agent_container._listen._start_failure_status import (
    DECLINED,
    SERVER_FAULT,
    STALE_SPEC,
    UNREGISTERED,
    classify_start_failure,
)

_MEASURED_CREDENTIAL_STDERR = (
    "Error: OAuth token in /home/ywatanabe/.claude/.credentials.json "
    "expired 257594 seconds ago. Run `claude login` to refresh."
)
_MEASURED_DRIFT_STDERR = (
    "sac-drift ERROR for agent 'business': the spec source is 3 commit(s) "
    "BEHIND origin/develop — you may be launching a STALE spec."
)


def test_an_unresolvable_agent_is_a_client_error():
    # Arrange
    stderr = "Error: cannot start 'scitex-scholar': its spec could not be loaded"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.status == 404


def test_an_unresolvable_agent_is_named_as_such():
    # Arrange
    stderr = "FileNotFoundError: Agent 'scitex-scholar' not found"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.kind == UNREGISTERED


def test_an_unresolvable_agent_is_reported_as_caller_fixable():
    # Arrange
    stderr = "Error: cannot start 'scitex-scholar': its spec could not be loaded"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.is_caller_fixable is True


def test_the_unregistered_hint_says_retrying_will_not_help():
    # Arrange
    stderr = "no such agent"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert "retrying unchanged will not help" in result.hint


def test_a_stale_spec_source_is_a_conflict_not_a_bad_request():
    # Arrange: the agent exists and the call is well-formed; the HOST is behind.
    stderr = _MEASURED_DRIFT_STDERR
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.status == 409


def test_a_stale_spec_source_is_named_as_such():
    # Arrange
    stderr = _MEASURED_DRIFT_STDERR
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.kind == STALE_SPEC


def test_the_stale_spec_hint_names_the_remedy():
    # Arrange
    stderr = _MEASURED_DRIFT_STDERR
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert "pull the spec repo" in result.hint


def test_a_self_declined_start_keeps_its_existing_status():
    # Arrange: an existing wire contract, and not caller-fixable — the
    # AGENT refused itself, so a 4xx would tell the caller to correct a
    # call that was already correct.
    # Act
    result = classify_start_failure(returncode=1, stderr="", declined=True)
    # Assert
    assert result.status == 502


def test_a_self_declined_start_is_named_as_such():
    # Arrange
    # Act
    result = classify_start_failure(returncode=1, stderr="", declined=True)
    # Assert
    assert result.kind == DECLINED


def test_an_unrecognised_failure_keeps_the_existing_status():
    # Arrange: the conservative default — unmatched must not become a guess.
    stderr = "apptainer: FATAL: while extracting image: root filesystem missing"
    # Act
    result = classify_start_failure(returncode=255, stderr=stderr)
    # Assert
    assert result.status == 502


def test_an_unrecognised_failure_is_named_a_server_fault():
    # Arrange
    stderr = "apptainer: FATAL: while extracting image"
    # Act
    result = classify_start_failure(returncode=255, stderr=stderr)
    # Assert
    assert result.kind == SERVER_FAULT


def test_a_server_fault_is_not_reported_as_caller_fixable():
    # Arrange: the harmful direction — never tell someone to fix a correct call.
    stderr = "apptainer: FATAL: while extracting image"
    # Act
    result = classify_start_failure(returncode=255, stderr=stderr)
    # Assert
    assert result.is_caller_fixable is False


def test_an_empty_stderr_falls_back_to_a_server_fault():
    # Arrange: silence must not be classified as a caller error.
    # Act
    result = classify_start_failure(returncode=1, stdout="", stderr="")
    # Assert
    assert result.status == 502


def test_the_measured_credential_failure_is_no_longer_a_bare_server_fault():
    # Arrange: hub's actual 502. Post-#1135 the child names the spec fault,
    # so the SAME start now classifies as unregistered rather than opaque.
    stderr = (
        "Error: cannot start 'scitex-scholar': its spec could not be loaded "
        "(FileNotFoundError: Agent 'scitex-scholar' not found)"
    )
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr)
    # Assert
    assert result.status == 404


def test_the_pre_fix_credential_message_alone_stays_a_server_fault():
    # Arrange: on its own the old message says nothing about the request,
    # so it must NOT be promoted to a caller error by pattern-matching hope.
    # Act
    result = classify_start_failure(
        returncode=1, stderr=_MEASURED_CREDENTIAL_STDERR
    )
    # Assert
    assert result.status == 502


def test_a_declined_start_outranks_a_matching_spec_pattern():
    # Arrange: an agent that declined, whose output also mentions a spec.
    stderr = "no such agent mentioned in passing"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr, declined=True)
    # Assert
    assert result.kind == DECLINED


def test_a_declined_start_is_not_promoted_to_a_client_error_by_a_stray_match():
    # Arrange: the early return is what stops the 404 pattern firing here.
    stderr = "no such agent mentioned in passing"
    # Act
    result = classify_start_failure(returncode=1, stderr=stderr, declined=True)
    # Assert
    assert result.is_caller_fixable is False
