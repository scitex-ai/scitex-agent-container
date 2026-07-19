#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: tests/scitex_agent_container/_provenance/test__identity_currency.py

"""``sac --version`` must report the RUNNING version, not the frozen claim.

This is the specific lie the operator hit daily for a week, and he was right
to call it serious: 「普通にバグなので重大なバグなので…私たちの認識を崩す
可能性があるからです」 — it corrupts shared understanding. Five sac installs
on one host reported 0.21.24 / 0.21.22 / 0.21.21 / 0.21.11 / none, and which
one you got depended on how you invoked it. His own editable `.venv`
advertised 0.21.21 while executing current develop, because
``importlib.metadata.version()`` reads a ``.dist-info`` frozen at
``pip install -e`` time that no ``git pull`` ever refreshes.

Two properties are asserted here:

* the number reported is the one derived from the CODE, and when the frozen
  metadata disagrees the disagreement is SHOWN rather than quietly resolved;
* the line names WHICH install and WHICH interpreter answered — without that
  a version report cannot be acted on at all.
"""

from __future__ import annotations

import sys

from scitex_agent_container._provenance import identity
from scitex_agent_container._provenance._identity import (
    format_terse,
    running_version,
)

# A wheel whose frozen metadata has fallen behind the code it sits beside.
FOSSIL_INFO = {
    "version": "0.21.24",
    "declared": "0.21.21",
    "version_source": "content",
    "executable": "/home/ywatanabe/proj/scitex-agent-container/.venv/bin/python",
    "commit": "a" * 40,
    "code_hash": None,
    "built_at": None,
    "install": "src",
    "origin": "/home/ywatanabe/proj/scitex-agent-container/src/scitex_agent_container",
}

# The same install, but the primitive was unavailable so the number is the
# unverified metadata claim.
UNVERIFIED_INFO = {
    **FOSSIL_INFO,
    "version": "0.21.21",
    "declared": "0.21.21",
    "version_source": "metadata",
}


class TestReportsTheRunningVersion:
    def test_the_running_version_is_labelled_with_its_provenance(self):
        # Arrange — a version whose provenance is unstated is how this
        # problem survived a week of being looked at.
        allowed = {"content", "metadata", "unknown"}
        # Act
        _version, source = running_version()
        # Assert
        assert source in allowed

    def test_the_version_is_content_verified_when_the_primitive_is_available(self):
        # Arrange — the contract is conditional on the checker being present,
        # and saying so is the whole point.
        #
        # This assertion originally read `source == "content"` unconditionally,
        # on the reasoning that the suite runs against a source checkout. That
        # was my DEV BOX described as a property of the code: CI installs
        # scitex-dev from PyPI, whose newest release (0.31.1) ships no
        # `versioning` module, so `running_version()` correctly falls back and
        # reports "metadata" — and the test failed the code for being right.
        # An environment accident asserted as a contract is exactly the shape
        # of bug this package exists to catch, so it does not get to live in
        # the tests either.
        from scitex_agent_container._freshness import available

        expected = "content" if available() else "metadata"
        # Act
        _version, source = running_version()
        # Assert
        assert source == expected

    def test_identity_reports_a_version(self):
        # Arrange
        info = identity()
        # Act
        version = info["version"]
        # Assert
        assert version

    def test_identity_keeps_the_declared_claim_alongside(self):
        # Arrange — the fossil is kept under a name that says what it is, so
        # the lie can be shown next to the truth instead of merely replaced.
        info = identity()
        # Act
        declared = info["declared"]
        # Assert
        assert declared

    def test_identity_names_the_interpreter_that_answered(self):
        # Arrange — which of five installs is speaking is decided by the
        # interpreter, since that is what differs between invocation paths
        # (login shell, direct argv, systemd, cron).
        info = identity()
        # Act
        executable = info["executable"]
        # Assert
        assert executable == sys.executable

    def test_identity_can_skip_the_content_probe_for_the_cheap_path(self):
        # Arrange — callers that need sub-millisecond identity may opt out,
        # but they must be told the number is unverified.
        info = identity(verify_content=False)
        # Act
        source = info["version_source"]
        # Assert
        assert source == "metadata"


class TestTerseLineStaysParseable:
    def test_the_version_is_still_the_third_whitespace_field(self):
        # Arrange — scripts already run `sac --version | cut -d' ' -f3`.
        # They keep working, and now they get the TRUE number, so the habit
        # stops being a trap.
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert line.split()[2] == "0.21.24"

    def test_the_line_still_names_where_the_module_was_loaded_from(self):
        # Arrange
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert f"from {info['origin']}" in line

    def test_the_line_carries_the_commit(self):
        # Arrange
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert "gaaaaaaaa" in line


class TestTerseLineNamesTheBinary:
    def test_the_line_names_the_interpreter(self):
        # Arrange — "0.21.21 is behind 0.21.24" is unusable without knowing
        # whose 0.21.21 it is.
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert f"(python {info['executable']})" in line

    def test_an_info_without_an_interpreter_still_renders(self):
        # Arrange — the field is additive; older callers must not crash.
        info = {k: v for k, v in FOSSIL_INFO.items() if k != "executable"}
        # Act
        line = format_terse(info)
        # Assert
        assert line.startswith("scitex-agent-container, version 0.21.24")


class TestTerseLineExposesTheFossil:
    def test_a_disagreeing_metadata_claim_is_shown_not_hidden(self):
        # Arrange — the disagreement IS the bug the operator kept hitting.
        # Seeing it once explains every confusing version report before it,
        # so it is stated rather than quietly corrected.
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert "metadata claims 0.21.21" in line

    def test_the_fossil_is_labelled_as_ignored(self):
        # Arrange
        info = FOSSIL_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert "fossil, ignored" in line

    def test_an_agreeing_metadata_claim_is_not_mentioned(self):
        # Arrange — no disagreement, no noise. An alarm that fires on the
        # healthy case is one people learn to skip.
        info = {**FOSSIL_INFO, "declared": "0.21.24"}
        # Act
        line = format_terse(info)
        # Assert
        assert "metadata claims" not in line

    def test_an_unverified_number_says_so(self):
        # Arrange — when the primitive was unavailable the number came from
        # the fossil path. Unlabelled, it is indistinguishable from a
        # verified one, and that indistinguishability is the whole problem.
        info = UNVERIFIED_INFO
        # Act
        line = format_terse(info)
        # Assert
        assert "unverified: metadata only" in line

    def test_a_verified_number_is_not_labelled_unverified(self):
        # Arrange
        info = {**FOSSIL_INFO, "declared": "0.21.24"}
        # Act
        line = format_terse(info)
        # Assert
        assert "unverified" not in line


# EOF
