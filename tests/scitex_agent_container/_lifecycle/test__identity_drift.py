"""An agent must not start silently holding two identities.

INCIDENT 2026-08-09 on scitex-compute-04. The sac maintainer ran with
agent name ``scitex-agent-container-04`` and ``SCITEX_TODO_AGENT_ID`` of
``scitex-agent-container`` — one process, two names, because the spec was
hand-made during a host migration.

Every consequence was SILENT:

* its card sweep, ``list_tasks(assignee=<board identity>)``, returned
  ``[]`` and it twice reported "board is clear, holding idle" while a P1
  with a full implementation brief sat runnable under the other name;
* its pull-inbox under the agent name accumulated notifications it never
  polled, including a card another agent filed for it;
* every ``reassign_task`` returned ``assignee_liveness: unknown``.

Nothing errored, because "no cards assigned to you" and "no cards
assigned to THIS SPELLING of you" render identically.

The guard is a WARNING, never a block — the same contract as the sibling
spec-source drift check. A mismatch is usually a migration artifact on an
agent that is otherwise working, and refusing to launch would strand it
rather than inform its operator. :func:`test_mismatch_does_not_raise`
pins that.

No mocks (PA-306): the function is pure over a spec-shaped object, and
the warning is observed through pytest's real ``caplog``. AAA markers,
one assertion per test.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from scitex_agent_container._lifecycle._identity_drift import (
    BOARD_IDENTITY_ENV,
    check_board_identity_at_launch,
)


def _config(name: str, board_id: str | None):
    env = {} if board_id is None else {BOARD_IDENTITY_ENV: board_id}
    return SimpleNamespace(name=name, env=env)


def test_mismatch_is_reported():
    # Arrange
    config = _config("scitex-agent-container-04", "scitex-agent-container")
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result == "scitex-agent-container"


def test_matching_identity_is_silent():
    # Arrange
    config = _config("scitex-cards", "scitex-cards")
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result is None


def test_absent_board_identity_is_not_a_mismatch():
    # Arrange: a spec setting no board identity inherits the runtime's,
    # which is the documented default path — not the failure guarded here.
    config = _config("scitex-db", None)
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result is None


def test_empty_board_identity_is_not_a_mismatch():
    # Arrange: an empty value is absence, not a second name.
    config = _config("scitex-db", "")
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result is None


def test_mismatch_does_not_raise():
    # Arrange: LOUD WARNING, never a block. Refusing to launch would
    # strand an agent that is otherwise working.
    config = _config("agent-a", "agent-b")
    # Act
    check_board_identity_at_launch(config)
    # Assert
    assert True


def test_unreadable_config_does_not_raise():
    # Arrange: a launch-time advisory must never crash the launch.
    config = object()
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result is None


def test_warning_names_both_identities(caplog):
    # Arrange
    config = _config("agent-a", "agent-b")
    # Act
    with caplog.at_level(logging.WARNING):
        check_board_identity_at_launch(config)
    # Assert
    assert "agent-a" in caplog.text and "agent-b" in caplog.text


def test_warning_points_at_the_rename_command(caplog):
    # Arrange: an error that only states what broke is half written. The
    # fix must not be done by hand — the rename migrates the cards.
    config = _config("agent-a", "agent-b")
    # Act
    with caplog.at_level(logging.WARNING):
        check_board_identity_at_launch(config)
    # Assert
    assert "sac agents rename" in caplog.text


def test_warning_says_the_agent_still_starts(caplog):
    # Arrange: the reader must not mistake an advisory for a boot failure.
    config = _config("agent-a", "agent-b")
    # Act
    with caplog.at_level(logging.WARNING):
        check_board_identity_at_launch(config)
    # Assert
    assert "STARTS NORMALLY" in caplog.text
