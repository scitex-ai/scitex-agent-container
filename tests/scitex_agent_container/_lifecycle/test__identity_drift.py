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
Recorded at the time as a fourth symptom, and WRONG: that every
``reassign_task`` returned ``assignee_liveness: unknown``. It does — for
every agent, drifted or not. Re-measured 2026-08-25 from an agent whose
identity was correct and which was demonstrably running: still
``unknown``. A field that reads the same in both states discriminates
nothing, so it is struck from the record rather than left to mislead the
next reader into "diagnosing" drift with it.

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
    BOARD_IDENTITY_ENV_RETIRED,
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


# ---------------------------------------------------------------------------
# The env var was renamed SCITEX_TODO_AGENT_ID -> SCITEX_CARDS_AGENT_ID and
# specs carry both spellings. Reading one only re-creates the very failure
# this module guards: on 2026-08-25 the check looked for the RETIRED name
# alone, so 110 of 148 specs on compute-04 read as "declares no board
# identity" and returned via the legitimate absent-identity branch.
# ---------------------------------------------------------------------------


def _config_under(name: str, env_key: str, board_id: str):
    return SimpleNamespace(name=name, env={env_key: board_id})


def test_drift_is_caught_under_the_current_env_name():
    # Arrange: the spelling 110 of 148 live specs actually use.
    config = _config_under("agent-a", "SCITEX_CARDS_AGENT_ID", "agent-b")
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result == "agent-b"


def test_drift_is_still_caught_under_the_retired_env_name():
    # Arrange: 21 live specs have not been migrated yet.
    config = _config_under("agent-a", "SCITEX_TODO_AGENT_ID", "agent-b")
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result == "agent-b"


def test_current_env_name_wins_when_a_spec_carries_both():
    # Arrange: a half-migrated spec. The current name is what the running
    # scitex_cards client reads, so it is the identity that has effect.
    config = SimpleNamespace(
        name="agent-a",
        env={
            BOARD_IDENTITY_ENV: "agent-current",
            BOARD_IDENTITY_ENV_RETIRED: "agent-retired",
        },
    )
    # Act
    result = check_board_identity_at_launch(config)
    # Assert
    assert result == "agent-current"


def test_warning_names_the_env_var_the_spec_declared(caplog):
    # Arrange: "fix your board identity" is unactionable when the spec has
    # two candidate keys and the message names neither.
    config = _config_under("agent-a", "SCITEX_TODO_AGENT_ID", "agent-b")
    # Act
    with caplog.at_level(logging.WARNING):
        check_board_identity_at_launch(config)
    # Assert
    assert "SCITEX_TODO_AGENT_ID" in caplog.text


def test_the_two_env_constants_are_not_the_same_string():
    # Arrange: a copy-paste that collapses them silently restores the
    # single-name blindness while every other test still passes.
    # Act
    same = BOARD_IDENTITY_ENV == BOARD_IDENTITY_ENV_RETIRED
    # Assert
    assert not same
