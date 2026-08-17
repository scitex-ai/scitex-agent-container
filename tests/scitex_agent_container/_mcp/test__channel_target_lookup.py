"""One zero, three causes — the send path's registry projections.

``delivered_subscriber_count == 0`` is identical for a detached adapter, a
stopped agent, and a name that was never registered, while the correct response
differs in each case. These are the pure functions that tell them apart, plus
the error that carries the third verdict.

The bias under test is uniform: an answer we could not read must degrade to the
PRE-EXISTING, safer verdict, never to a confident new one.
"""

from __future__ import annotations

from scitex_agent_container._listen._inbox_fault import (
    FAULT_DEAF_INBOX,
    FAULT_NOT_RUNNING,
)
from scitex_agent_container._mcp._channel_send_errors import (
    ERR_TARGET_NOT_RUNNING,
    not_running_error,
)
from scitex_agent_container._mcp._channel_target_lookup import (
    fault_of,
    is_registered,
    names_of,
    rows_from_agents_body,
)

LIVE = {"name": "alpha", "fault": None}
DEAF = {"name": "beta", "fault": FAULT_DEAF_INBOX}
STOPPED = {"name": "gamma", "fault": FAULT_NOT_RUNNING}


# --- parsing the /agents body --------------------------------------------


def test_current_envelope_is_parsed():
    # Arrange
    body = {"agents": [LIVE, STOPPED]}
    # Act
    rows = rows_from_agents_body(body)
    # Assert
    assert rows == [LIVE, STOPPED]


def test_bare_list_from_an_older_listen_is_parsed():
    # Arrange
    body = [LIVE]
    # Act
    rows = rows_from_agents_body(body)
    # Assert
    assert rows == [LIVE]


def test_bare_name_strings_are_promoted_to_rows():
    """An old daemon listing names must not read as an empty fleet."""
    # Arrange
    body = ["alpha", "beta"]
    # Act
    rows = rows_from_agents_body(body)
    # Assert
    assert rows == [{"name": "alpha"}, {"name": "beta"}]


def test_unparseable_body_yields_no_rows():
    # Arrange
    body = "not a listing"
    # Act
    rows = rows_from_agents_body(body)
    # Assert
    assert rows == []


# --- is the name real? ----------------------------------------------------


def test_a_listed_name_is_registered():
    # Arrange
    rows = [LIVE, STOPPED]
    # Act
    registered = is_registered("alpha", rows)
    # Assert
    assert registered is True


def test_an_absent_name_is_not_registered():
    """The `sac-04` case: a typo must be named, not queued forever."""
    # Arrange
    rows = [LIVE, STOPPED]
    # Act
    registered = is_registered("sac-04", rows)
    # Assert
    assert registered is False


def test_an_unreadable_registry_does_not_accuse_the_name():
    """"I could not check" is not evidence of a bad name.

    An empty list means the registry was unreadable OR genuinely empty, and
    convicting the caller's spelling off that would invent false certainty.
    """
    # Arrange
    rows: list[dict] = []
    # Act
    registered = is_registered("alpha", rows)
    # Assert
    assert registered is True


def test_names_are_read_from_the_agent_key_too():
    # Arrange
    rows = [{"agent": "delta"}]
    # Act
    names = names_of(rows)
    # Assert
    assert names == ["delta"]


# --- is the agent running? ------------------------------------------------


def test_a_stopped_target_reports_the_not_running_fault():
    # Arrange
    rows = [LIVE, STOPPED]
    # Act
    fault = fault_of("gamma", rows)
    # Assert
    assert fault == FAULT_NOT_RUNNING


def test_a_deaf_target_reports_the_deaf_fault():
    """A live-but-deaf target keeps the WAIT advice — its adapter will return."""
    # Arrange
    rows = [LIVE, DEAF]
    # Act
    fault = fault_of("beta", rows)
    # Assert
    assert fault == FAULT_DEAF_INBOX


def test_an_older_listen_without_the_field_reports_no_fault():
    """No ``fault`` key means fall back to the verdict that was always safe."""
    # Arrange
    rows = [{"name": "alpha"}]
    # Act
    fault = fault_of("alpha", rows)
    # Assert
    assert fault is None


def test_an_unlisted_target_reports_no_fault():
    # Arrange
    rows = [LIVE]
    # Act
    fault = fault_of("nobody", rows)
    # Assert
    assert fault is None


# --- the error the stopped case raises ------------------------------------


def test_not_running_error_carries_its_own_failure_code():
    """A distinct code so callers branch on the CLASS, not on prose."""
    # Arrange
    target = "gamma"
    # Act
    err = not_running_error(target)
    # Assert
    assert err.code == ERR_TARGET_NOT_RUNNING


def test_not_running_error_still_reports_the_message_as_queued():
    """It IS durable — it just will not be delivered until someone starts them."""
    # Arrange
    target = "gamma"
    # Act
    err = not_running_error(target)
    # Assert
    assert err.detail["durably_queued"] is True


def test_not_running_error_says_the_target_is_not_running():
    # Arrange
    target = "gamma"
    # Act
    err = not_running_error(target)
    # Assert
    assert err.detail["target_running"] is False


def test_not_running_error_tells_the_sender_not_to_wait():
    """The inversion this fault exists to fix: no reconnect is coming."""
    # Arrange
    target = "gamma"
    # Act
    err = not_running_error(target)
    # Assert
    assert any("DO NOT WAIT" in step for step in err.detail["what_to_do"])


def test_not_running_error_does_not_tell_the_sender_to_start_the_agent():
    """Starting someone else's agent is an operator decision, not a side effect."""
    # Arrange
    target = "gamma"
    # Act
    err = not_running_error(target)
    # Assert
    assert any("Do NOT start the target" in step for step in err.detail["what_to_do"])
