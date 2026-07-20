"""Rotations and their consequences must read as ONE ordered story.

THE FIXTURE IS THE REAL WRITER
    Every rotation record in this suite is produced by the PRODUCTION
    :func:`scitex_agent_container._account._rotation_audit.log_rotation_event`,
    never hand-built. A hand-built fixture would only prove that my parser
    agrees with my idea of the format — and a test whose input I shaped to
    match my parser cannot disagree with me. Driving the real writer is what
    makes a schema drift between the two modules show up here as a failure
    instead of showing up during the next incident.

WHAT THIS BUYS
    The 2026-07-18 deaths clustered at 10:31 UTC. The rotation that caused them
    was already on disk at 10:31:28 UTC — in a different file, so nobody put
    the two side by side. Ordering them into one timeline is the whole fix; it
    needs no new emitter, because the emitter already existed.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._account import _rotation_audit as ra
from scitex_agent_container._authevents import (
    AUTH_FAILURE_OBSERVED,
    TOKEN_ROTATED,
    log_auth_failure_observed,
    rotation_events,
    unified_timeline,
)

from ._helpers import NOW


def _rotate(store: Path, *, reason: str, now: float, to: str = "ywata1989") -> None:
    """Write ONE rotation record with the real production writer."""
    ra.log_rotation_event(
        store=store,
        event="refresh",
        from_account=to,
        to_account=to,
        reason=reason,
        now=now,
    )


def test_a_real_rotation_record_projects_into_the_shared_event_shape(
    tmp_path: Path,
) -> None:
    """The projection must read what the production writer actually writes."""
    # Arrange
    store = tmp_path / "accounts"

    # Act
    _rotate(store, reason="single-use refresh_token rotated", now=NOW)

    # Assert
    events = rotation_events(store / ra.AUDIT_FILENAME)
    assert [e.event for e in events] == [TOKEN_ROTATED]


def test_the_rotated_account_survives_the_projection(tmp_path: Path) -> None:
    """The account is the field the whole correlation hangs on.

    "Six died at once — whose token rotated?" is unanswerable if the projected
    record forgets which account it was about.
    """
    # Arrange
    store = tmp_path / "accounts"

    # Act
    _rotate(store, reason="headless access-token refresh", now=NOW, to="ywata1989")

    # Assert
    assert rotation_events(store / ra.AUDIT_FILENAME)[0].account == "ywata1989"


def test_the_rotation_reason_survives_the_projection(tmp_path: Path) -> None:
    """WHY it rotated distinguishes a scheduled refresh from a reaction."""
    # Arrange
    store = tmp_path / "accounts"
    reason = "single-use refresh_token rotated (headless access-token refresh)"

    # Act
    _rotate(store, reason=reason, now=NOW)

    # Assert
    assert rotation_events(store / ra.AUDIT_FILENAME)[0].detail == reason


def test_a_rotation_belongs_to_no_single_agent(tmp_path: Path) -> None:
    """A rotation hits every co-tenant at once, so its ``agent`` is null.

    Attributing it to one agent would suggest the others were separate
    incidents — which is exactly the misreading that cost hours.
    """
    # Arrange
    store = tmp_path / "accounts"

    # Act
    _rotate(store, reason="refresh", now=NOW)

    # Assert
    assert rotation_events(store / ra.AUDIT_FILENAME)[0].agent is None


def test_the_timeline_orders_a_rotation_before_the_failures_it_caused(
    tmp_path: Path,
) -> None:
    """The causal shape of the incident, reconstructed from two files.

    Rotation first, then the agents noticing. That ordering IS the answer the
    operator could not get on the night, and it comes from a single read.
    """
    # Arrange
    store = tmp_path / "accounts"
    event_log = tmp_path / "auth-events.jsonl"
    _rotate(store, reason="single-use refresh_token rotated", now=NOW)

    # Act
    for agent in ("figrecipe", "crossref-local"):
        log_auth_failure_observed(
            agent=agent, detail="banner", path=event_log, now=NOW + 60
        )

    # Assert
    timeline = unified_timeline(
        events_path=event_log, audit_path=store / ra.AUDIT_FILENAME
    )
    assert [e.event for e in timeline] == [
        TOKEN_ROTATED,
        AUTH_FAILURE_OBSERVED,
        AUTH_FAILURE_OBSERVED,
    ]


def test_the_timeline_can_be_read_without_rotations(tmp_path: Path) -> None:
    """The join is opt-out, so the auth events can be read on their own."""
    # Arrange
    store = tmp_path / "accounts"
    event_log = tmp_path / "auth-events.jsonl"
    _rotate(store, reason="refresh", now=NOW)

    # Act
    log_auth_failure_observed(
        agent="figrecipe", detail="banner", path=event_log, now=NOW + 60
    )

    # Assert
    timeline = unified_timeline(
        events_path=event_log,
        audit_path=store / ra.AUDIT_FILENAME,
        include_rotations=False,
    )
    assert [e.event for e in timeline] == [AUTH_FAILURE_OBSERVED]


def test_a_missing_rotation_audit_yields_no_rotations(tmp_path: Path) -> None:
    """An absent audit reads as empty, never as an exception.

    Note what the empty answer does NOT mean: it is not evidence that no
    rotation happened, only that we have no record of one here.
    """
    # Arrange
    absent = tmp_path / "accounts" / ra.AUDIT_FILENAME

    # Act
    events = rotation_events(absent)

    # Assert
    assert events == []


def test_the_timeline_survives_a_corrupt_rotation_line(tmp_path: Path) -> None:
    """One bad line must not cost the whole rotation history."""
    # Arrange
    store = tmp_path / "accounts"
    _rotate(store, reason="first", now=NOW)
    with (store / ra.AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write("{truncated mid-write\n")

    # Act
    _rotate(store, reason="second", now=NOW + 1)

    # Assert
    details = [e.detail for e in rotation_events(store / ra.AUDIT_FILENAME)]
    assert details == ["first", "second"]


def test_a_record_with_no_timestamp_sorts_last_instead_of_vanishing(
    tmp_path: Path,
) -> None:
    """A malformed record is still evidence that something wrote one.

    Dropping it would make the timeline quietly shorter than the truth, which
    is the failure mode an investigator has no way to notice.
    """
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    log_auth_failure_observed(
        agent="figrecipe", detail="banner", path=event_log, now=NOW
    )
    with event_log.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "auth-failure-observed", "agent": "nostamp"}\n')

    # Act
    timeline = unified_timeline(events_path=event_log, include_rotations=False)

    # Assert
    assert [e.agent for e in timeline] == ["figrecipe", "nostamp"]
