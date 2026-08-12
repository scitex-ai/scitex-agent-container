"""A zero must name its cause — deaf vs stopped vs "I could not look".

The rules under test are the ones this fleet has already broken twice: a wall of
``inbox_subscribers: 0`` read as a deaf fleet (2026-07-14, a P0 that did not
exist), and again on 2026-08-12 when 9 of 15 rows on one host were STOPPED
agents reported as a reachability decay.

Every case drives :func:`classify_fault` with a hand-rolled snapshot and a
hand-rolled runtime resolver, so the RULE is proved without a tmux server, a
listen daemon, an on-disk spec, or a live agent.
"""

from __future__ import annotations

from scitex_agent_container._listen._inbox_fault import (
    FAULT_DEAF_INBOX,
    FAULT_NOT_RUNNING,
    annotate_faults,
    classify_fault,
)
from scitex_agent_container._listen._reachability import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
)

# Hand-rolled runtime resolvers — the three answers the real spec lookup can
# give. Injected rather than patched: the seam is production's own parameter.
TUI = lambda name: True  # noqa: E731 - a one-line stub reads better inline
APPTAINER = lambda name: False  # noqa: E731
UNRESOLVABLE = lambda name: None  # noqa: E731


def _row(name="alpha", *, subscribers=0, reachable=UNREACHABLE):
    """A ``GET /agents`` row, defaulting to the ambiguous zero under test."""
    return {
        "name": name,
        "inbox_subscribers": subscribers,
        "inbox_reachable": reachable,
    }


# --- THE fault this module exists to name --------------------------------


def test_live_session_with_zero_subscribers_is_deaf():
    """Session observed + 0 subscribers = RUNNING BUT DEAF.

    The state that was previously unnameable: green at every surface, work
    routed to it, work evaporates.
    """
    # Arrange
    row = _row()
    # Act
    fault = classify_fault(row, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault == FAULT_DEAF_INBOX


def test_live_session_with_a_subscriber_is_no_fault():
    # Arrange
    row = _row(subscribers=1, reachable=REACHABLE)
    # Act
    fault = classify_fault(row, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


# --- the confound: a stopped agent is NOT a deaf one ----------------------


def test_absent_session_is_reported_as_not_running():
    """The 2026-08-12 case: no session, and the registry row outlived it.

    Reporting this as a deaf inbox is the misdiagnosis; the remedy is the
    opposite one, because waiting for a reconnect can never work.
    """
    # Arrange
    row = _row()
    # Act
    fault = classify_fault(row, snapshot={"tui-other": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault == FAULT_NOT_RUNNING


def test_stopped_agent_is_distinguished_from_a_deaf_one():
    """Same zero, same ``unreachable`` label, different verdict.

    These two rows are byte-identical on every field the old surface
    published — which is exactly why nine stopped agents read as deaf ones.
    """
    # Arrange
    snapshot = {"tui-live": 1}
    # Act
    verdicts = (
        classify_fault(_row("live"), snapshot=snapshot, runtime_is_tui_fn=TUI),
        classify_fault(_row("gone"), snapshot=snapshot, runtime_is_tui_fn=TUI),
    )
    # Assert
    assert verdicts == (FAULT_DEAF_INBOX, FAULT_NOT_RUNNING)


# --- rule 1: only a POSITIVE observation convicts -------------------------


def test_unobservable_snapshot_convicts_nobody():
    """``snapshot=None`` is "I could not look" — it must accuse nobody.

    A container cannot see the host's tmux; rendering that blindness as
    absence would slander every row at once.
    """
    # Arrange
    row = _row()
    # Act
    fault = classify_fault(row, snapshot=None, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


def test_apptainer_agent_without_a_tmux_session_is_not_convicted():
    """A non-TUI runtime holds no tmux session BY CONSTRUCTION.

    Convicting on its absence is the old pidfile probe's bug run backwards —
    that one stamped ``startup_failed`` on every healthy TUI agent.
    """
    # Arrange
    row = _row()
    # Act
    fault = classify_fault(row, snapshot={}, runtime_is_tui_fn=APPTAINER)
    # Assert
    assert fault is None


def test_unresolvable_runtime_is_not_convicted():
    """An unreadable spec is not evidence that an agent is gone."""
    # Arrange
    row = _row()
    # Act
    fault = classify_fault(row, snapshot={}, runtime_is_tui_fn=UNRESOLVABLE)
    # Assert
    assert fault is None


def test_unknown_reachability_row_is_skipped():
    """``inbox_reachable: unknown`` already means "not ours to observe".

    Reusing it keeps the locality rule in the one module that owns it.
    """
    # Arrange
    row = _row(subscribers=None, reachable=UNKNOWN)
    # Act
    fault = classify_fault(row, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


def test_row_without_a_name_is_skipped():
    # Arrange
    row = {"inbox_subscribers": 0, "inbox_reachable": UNREACHABLE}
    # Act
    fault = classify_fault(row, snapshot={}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


def test_missing_subscriber_count_does_not_convict():
    """An absent count is not a zero — we observed nothing."""
    # Arrange
    row = {"name": "alpha", "inbox_reachable": UNREACHABLE}
    # Act
    fault = classify_fault(row, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


def test_a_live_subscriber_outranks_a_missing_session():
    """When the two instruments disagree, the POSITIVE reading wins.

    The broker watching something attach is first-hand proof a process is
    home; "no session named tui-<name>" is an absence, and an absence must
    never beat a positive observation. Such an agent is neither deaf nor gone.
    """
    # Arrange
    row = _row(subscribers=1, reachable=REACHABLE)
    # Act
    fault = classify_fault(row, snapshot={}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


def test_bool_subscriber_count_does_not_convict():
    """``False`` is not a subscriber count of zero."""
    # Arrange
    row = _row(subscribers=False)
    # Act
    fault = classify_fault(row, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert fault is None


# --- the row overlay ------------------------------------------------------


def test_healthy_row_still_carries_the_fault_key():
    """Uniform shape: consumers branch on the VALUE, not on the key existing."""
    # Arrange
    rows = [_row("live", subscribers=1, reachable=REACHABLE)]
    # Act
    out = annotate_faults(rows, snapshot={"tui-live": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert out[0]["fault"] is None


def test_healthy_row_carries_no_fault_detail():
    # Arrange
    rows = [_row("live", subscribers=1, reachable=REACHABLE)]
    # Act
    out = annotate_faults(rows, snapshot={"tui-live": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert "fault_detail" not in out[0]


def test_deaf_row_is_told_not_to_re_send():
    """A named fault whose remedy must be guessed still ends in a wrong action."""
    # Arrange
    rows = [_row()]
    # Act
    out = annotate_faults(rows, snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert "do not re-send" in out[0]["fault_detail"].lower()


def test_not_running_row_is_not_told_to_wait_for_a_reconnect():
    """The stopped case must contradict, not echo, the deaf case's advice."""
    # Arrange
    rows = [_row()]
    # Act
    out = annotate_faults(rows, snapshot={}, runtime_is_tui_fn=TUI)
    # Assert
    assert "outlived the process" in out[0]["fault_detail"]


def test_annotate_preserves_every_pre_existing_field():
    """Additive overlay: the declaration is never overwritten by the observation."""
    # Arrange
    row = {**_row(), "pid": 4242, "a2a_port": 19000, "role": "maintainer"}
    # Act
    out = annotate_faults([row], snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)[0]
    # Assert
    assert all(out[key] == value for key, value in row.items())


def test_annotate_does_not_mutate_the_input_row():
    # Arrange
    row = _row()
    # Act
    annotate_faults([row], snapshot={"tui-alpha": 1}, runtime_is_tui_fn=TUI)
    # Assert
    assert "fault" not in row
