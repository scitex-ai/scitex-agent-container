"""Tests for predictable job logging.

The gap being closed: sac's jobs are not dispatched through
``ecosystem cron exec``, so scitex-dev's log sink never installs for them
and their output goes to the journal under a unit name that CHANGES WITH
EVERY RENAME. "Where did last night's worktree-gc go" had no answer that
survived a migration.

The load-bearing test here is
``test_the_dropin_path_matches_scitex_devs_own``, which calls the REAL
scitex-dev resolver. Inventing a second log-tree convention would make
sac's logs predictable and the ecosystem's inconsistent — a second place
to look is not an improvement. That test makes an upstream convention
change fail sac's build instead of silently splitting the tree in two.

No mocks (PA-306): pure string arithmetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._jobs._migrate import _logsink


def test_the_slug_strips_the_canonical_package_prefix() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.log_slug(name, "timer")
    # Assert
    assert got == "timer-worktree-gc"


def test_the_slug_strips_the_legacy_prefix_too() -> None:
    # Arrange — the held refresher still carries `sac.`, and its log must
    # land beside the others rather than under a mangled name.
    name = "sac.accounts-refresh"
    # Act
    got = _logsink.log_slug(name, "timer")
    # Assert
    assert got == "timer-accounts-refresh"


def test_the_slug_carries_the_kind() -> None:
    # Arrange — following scitex-dev's own slugs (timer-…, cron-…), so a
    # directory listing says what kind of thing wrote each file.
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.log_slug(name, "cron")
    # Assert
    assert got.startswith("cron-")


def test_an_empty_kind_is_rejected() -> None:
    # Arrange
    # Act
    def _call():
        return _logsink.log_slug("scitex-agent-container-x", "")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_a_name_with_no_local_part_is_rejected() -> None:
    # Arrange — a bare prefix would produce `timer-.log`, which names
    # nothing and silently collides with every other such job.
    # Act
    def _call():
        return _logsink.log_slug("scitex-agent-container-", "timer")

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_the_log_dir_uses_the_systemd_home_specifier() -> None:
    # Arrange — a drop-in must be correct for whichever user systemd
    # expands it as, so a resolved home would be wrong on every other host.
    # Act
    got = _logsink.LOG_DIR
    # Assert
    assert got.startswith("%h/")


def test_the_log_path_composes_the_dir_and_the_slug() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.log_path(name, "timer")
    # Assert
    assert got == _logsink.LOG_DIR + "/timer-worktree-gc.log"


def test_the_dropin_path_matches_scitex_devs_own() -> None:
    # Arrange — THE test that keeps one convention instead of two. Calls
    # the REAL upstream resolver; if scitex-dev moves its log tree, this
    # fails here rather than splitting the tree silently.
    from scitex_dev.jobs._logsink import log_path as upstream_log_path

    expected = upstream_log_path(
        _logsink.LOG_PACKAGE, "timer-worktree-gc", home=Path("/h")
    )
    # Act
    got = _logsink.log_path("scitex-agent-container-worktree-gc", "timer").replace(
        "%h", "/h"
    )
    # Assert
    assert got == str(expected)


def test_the_dropin_declares_a_service_section() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.logging_dropin_text(name, "timer")
    # Assert
    assert "[Service]" in got


def test_the_dropin_appends_rather_than_truncating() -> None:
    # Arrange — `file:` would truncate on every restart, discarding the
    # history the operator came to read.
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.logging_dropin_text(name, "timer")
    # Assert
    assert "StandardOutput=append:" in got


def test_the_dropin_never_uses_the_truncating_file_directive() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.logging_dropin_text(name, "timer")
    # Assert
    assert "=file:" not in got


def test_stdout_and_stderr_land_in_the_same_file() -> None:
    # Arrange — a job's error output is worthless separated from the
    # output that led to it.
    name = "scitex-agent-container-worktree-gc"
    path = _logsink.log_path(name, "timer")
    # Act
    got = _logsink.logging_dropin_text(name, "timer")
    # Assert
    assert got.count(path) == 2


def test_the_dropin_says_what_manages_it() -> None:
    # Arrange — an unexplained file under <unit>.d/ is one an operator
    # deletes.
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _logsink.logging_dropin_text(name, "timer")
    # Assert
    assert "migrate-job-names" in got
