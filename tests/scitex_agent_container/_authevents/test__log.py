"""The auth-event log records facts, and can record a fact that REFUTES us.

The suite's centre of gravity is :func:`unresolved_attempts`. A restart log
that can only say "restarted" is not evidence, because there is no reading of
it under which the restarter looks wrong — and the fleet spent seven days with
exactly that log while 169 restarts failed to clear anything. So the tests
below insist an ATTEMPT and an OUTCOME are two records, and that the outcome is
free to contradict the attempt.

No mocks: every test drives the production writer against a real file on
``tmp_path`` and reads the real bytes back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scitex_agent_container._authevents import (
    AUTH_FAILURE_OBSERVED,
    RESTART_ATTEMPTED,
    RESTART_OUTCOME,
    auth_event_log_path,
    log_auth_event,
    log_auth_failure_observed,
    log_restart_attempted,
    log_restart_outcome,
    read_auth_events,
    unresolved_attempts,
)

from ._helpers import NOW, NOW_ISO


def test_attempt_and_outcome_are_two_distinct_records(event_log: Path) -> None:
    """THE contract: one restart leaves an attempt AND an outcome, not one line.

    If the code ever collapses these into a single "restarted" event this test
    goes red — which is the point. The two-record shape is what lets a later
    reader ASK whether the restart worked, instead of being TOLD that it did.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=event_log, now=NOW
    )
    log_restart_outcome(
        agent=agent,
        attempt_id=attempt_id,
        succeeded=True,
        detail="restart call reported success",
        path=event_log,
        now=NOW + 1,
    )

    # Assert
    events = read_auth_events(event_log)
    assert [e.event for e in events] == [RESTART_ATTEMPTED, RESTART_OUTCOME]


def test_attempt_and_outcome_are_joined_by_attempt_id(event_log: Path) -> None:
    """The join key makes two records one story rather than two rumours."""
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=event_log, now=NOW
    )
    log_restart_outcome(
        agent=agent,
        attempt_id=attempt_id,
        succeeded=True,
        detail="ok",
        path=event_log,
        now=NOW + 1,
    )

    # Assert
    attempt, outcome = read_auth_events(event_log)
    assert attempt.attempt_id == outcome.attempt_id == attempt_id


def test_a_failed_restart_is_recorded_as_attempt_without_success(
    event_log: Path,
) -> None:
    """REFUTATION: a restart that ran and did not work must be readable as such.

    This is the case the old ``-> auto-restart`` line could not express: the
    outcome exists, and it disagrees with the attempt.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=event_log, now=NOW
    )
    log_restart_outcome(
        agent=agent,
        attempt_id=attempt_id,
        succeeded=False,
        detail="restart ran but reported FAILURE",
        path=event_log,
        now=NOW + 1,
    )

    # Assert
    unresolved = unresolved_attempts(read_auth_events(event_log))
    assert [e.attempt_id for e in unresolved] == [attempt_id]


def test_an_attempt_with_no_outcome_at_all_is_unresolved(event_log: Path) -> None:
    """Silence is not success. A restart that never reported back stays open.

    The process could have hung or been killed by its timer. Reading a missing
    outcome as a successful one is the assumption that lets a wedge sit for
    three days while every log line announces a remedy.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=event_log, now=NOW
    )

    # Assert
    unresolved = unresolved_attempts(read_auth_events(event_log))
    assert [e.attempt_id for e in unresolved] == [attempt_id]


def test_a_successful_outcome_clears_its_attempt(event_log: Path) -> None:
    """The query must also be able to say NOTHING is outstanding.

    A refutation test that can never come back clean is not measuring anything.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=event_log, now=NOW
    )
    log_restart_outcome(
        agent=agent,
        attempt_id=attempt_id,
        succeeded=True,
        detail="ok",
        path=event_log,
        now=NOW + 1,
    )

    # Assert
    assert unresolved_attempts(read_auth_events(event_log)) == []


def test_one_agents_success_does_not_clear_another_agents_attempt(
    event_log: Path,
) -> None:
    """Attempts are cleared by THEIR own outcome, never by a neighbour's.

    Six agents wedged at once is the shape that matters here; a rail that let
    any success clear the batch would have reported 2026-07-18 as handled.
    """
    # Arrange
    healed = log_restart_attempted(
        agent="figrecipe", detail="wedged", path=event_log, now=NOW
    )
    still_wedged = log_restart_attempted(
        agent="crossref-local", detail="wedged", path=event_log, now=NOW
    )

    # Act
    log_restart_outcome(
        agent="figrecipe",
        attempt_id=healed,
        succeeded=True,
        detail="ok",
        path=event_log,
        now=NOW + 1,
    )

    # Assert
    unresolved = unresolved_attempts(read_auth_events(event_log))
    assert [e.attempt_id for e in unresolved] == [still_wedged]


def test_unknown_account_is_written_as_null_not_omitted(event_log: Path) -> None:
    """Tri-state: the field is PRESENT and null. Absent and unknown differ.

    An absent key says nobody thought to record it; a null says we looked and
    could not tell. Only the second one is a finding.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(
        agent=agent, detail="banner", account=None, path=event_log, now=NOW
    )

    # Assert
    record = json.loads(event_log.read_text().strip())
    assert "account" in record and record["account"] is None


def test_the_literal_unknown_label_is_normalised_to_null(event_log: Path) -> None:
    """A non-answer must not travel as though it were an answer.

    ``resolve_agent_account_label`` says the string ``"unknown"`` when it has
    nothing to go on. Writing that verbatim would put a joinable-looking value
    into the field investigators correlate rotations against.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(
        agent=agent, detail="banner", account="unknown", path=event_log, now=NOW
    )

    # Assert
    record = json.loads(event_log.read_text().strip())
    assert record["account"] is None


def test_a_known_account_is_recorded_verbatim(event_log: Path) -> None:
    """The normaliser must not eat real answers along with the non-answers."""
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(
        agent=agent,
        detail="banner",
        account="ywata1989-gmail-com",
        path=event_log,
        now=NOW,
    )

    # Assert
    assert read_auth_events(event_log)[0].account == "ywata1989-gmail-com"


def test_http_status_is_null_when_none_was_observed(event_log: Path) -> None:
    """A banner is a rendering; a status code is a fact. Never synthesise one.

    Claude Code prints "Login expired · Please run /login" for ANY 401 — and
    sometimes when nothing expired. Inventing a 401 from that string would
    corrupt the one field this log exists to make trustworthy.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(agent=agent, detail="banner", path=event_log, now=NOW)

    # Assert
    record = json.loads(event_log.read_text().strip())
    assert "http_status" in record and record["http_status"] is None


def test_an_observed_status_code_is_recorded(event_log: Path) -> None:
    """When a real status IS observed it must survive to the record."""
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(
        agent=agent,
        detail="401 from the API",
        http_status=401,
        path=event_log,
        now=NOW,
    )

    # Assert
    assert read_auth_events(event_log)[0].http_status == 401


def test_the_write_fails_open_when_the_log_cannot_be_written(
    denied_log: Path,
) -> None:
    """FAIL-OPEN: an unwritable log returns False and never raises.

    Driven against a really read-only directory. This rail is bolted to the
    side of the auth path; if it could raise, an observability bug would become
    an outage — and the thing observed always outranks the observing of it.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    written = log_auth_failure_observed(
        agent=agent, detail="banner", path=denied_log, now=NOW
    )

    # Assert
    assert written is False


def test_an_attempt_id_is_returned_even_when_the_write_failed(
    denied_log: Path,
) -> None:
    """The caller must still be able to label its outcome after a failed write.

    Returning nothing here would let the recorder's own failure change the
    shape of the restart path around it — which is what fail-open forbids.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    attempt_id = log_restart_attempted(
        agent=agent, detail="wedged", path=denied_log, now=NOW
    )

    # Assert
    assert attempt_id


def test_reading_a_log_that_does_not_exist_yields_no_events(tmp_path: Path) -> None:
    """A missing log reads as empty, never as an exception."""
    # Arrange
    absent = tmp_path / "never-written.jsonl"

    # Act
    events = read_auth_events(absent)

    # Assert
    assert events == []


def test_a_corrupt_line_does_not_hide_the_good_records_around_it(
    event_log: Path,
) -> None:
    """A half-written line must cost one record, not the whole investigation."""
    # Arrange
    log_auth_failure_observed(
        agent="figrecipe", detail="banner", path=event_log, now=NOW
    )
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")

    # Act
    log_auth_failure_observed(
        agent="crossref-local", detail="banner", path=event_log, now=NOW
    )

    # Assert
    events = read_auth_events(event_log)
    assert [e.agent for e in events] == ["figrecipe", "crossref-local"]


def test_records_are_appended_never_overwritten(event_log: Path) -> None:
    """Append-only: a later write must not cost us an earlier one."""
    # Arrange
    agents = ("a", "b", "c")

    # Act
    for agent in agents:
        log_auth_failure_observed(agent=agent, detail="banner", path=event_log, now=NOW)

    # Assert
    assert [e.agent for e in read_auth_events(event_log)] == list(agents)


def test_the_timestamp_is_utc_and_iso_8601(event_log: Path) -> None:
    """Cross-host correlation needs one clock, spelled one way."""
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(agent=agent, detail="banner", path=event_log, now=NOW)

    # Assert
    assert read_auth_events(event_log)[0].timestamp_utc == NOW_ISO


def test_an_unrecognised_event_type_is_recorded_and_flagged(event_log: Path) -> None:
    """Forward-compat: a record we do not recognise is still evidence.

    Dropping it would let a newer writer's events vanish silently; keeping it
    unmarked would hide a typo. So it is kept AND flagged.
    """
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_event(
        event="something-new", agent=agent, detail="?", path=event_log, now=NOW
    )

    # Assert
    assert read_auth_events(event_log)[0].raw["event_known"] is False


def test_the_log_path_honours_its_env_override(tmp_path: Path) -> None:
    """Resolved per call, so a relocation (or a test) takes effect immediately.

    A module-level constant computed at import cannot be redirected afterwards
    — the shape that once had a suite reading and writing the REAL fleet store
    while believing it was hermetic.
    """
    # Arrange
    target = tmp_path / "relocated" / "auth-events.jsonl"
    saved = os.environ.get("SAC_AUTH_EVENT_LOG")
    os.environ["SAC_AUTH_EVENT_LOG"] = str(target)

    # Act
    try:
        resolved = auth_event_log_path()
    finally:
        if saved is None:
            os.environ.pop("SAC_AUTH_EVENT_LOG", None)
        else:
            os.environ["SAC_AUTH_EVENT_LOG"] = saved

    # Assert
    assert resolved == target


def test_an_observation_records_which_agent_it_is_about(event_log: Path) -> None:
    """Sanity: the observation event carries its own type and its subject."""
    # Arrange
    agent = "figrecipe"

    # Act
    log_auth_failure_observed(agent=agent, detail="banner", path=event_log, now=NOW)

    # Assert
    event = read_auth_events(event_log)[0]
    assert (event.event, event.agent) == (AUTH_FAILURE_OBSERVED, agent)
