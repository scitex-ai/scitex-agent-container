"""A pane tail is CONTEXT, so it must not be logged as a fault.

Operator report, 2026-07-23: a SUCCESSFUL ``sac-start grant`` read as a wall
of failures, because the boot fault's log record carried the target session's
pane tail inline and the formatter stamped ``ERRO:`` onto every one of its
lines::

    sac-start ERRO: TuiSessionRuntime: stale compose buffer ... Pane tail:
    ERRO: ✻ Running scheduled task (Jul 23 1:25am)
    ERRO: ❯ /compact
    SUCC: grant started

The condition itself is real and stays at ERROR. What moves is the
transcription of ANOTHER session's screen: its own record, at INFO.

...and the evidence must SURVIVE that move (operator, 2026-08-08, card
``sac-boot-failure-drops-its-own-pane-evidence-20260808``). ``sac-start``
renders at a level that shows ERRO and drops INFO, so demoting the pane deleted
it from the only output the operator reads::

    ERRO: TuiSessionRuntime: stale compose buffer for tui-... did NOT clear ...
    ERRO: TuiSessionRuntime: startup_prompt ... stayed pasted-but-UNSENT ...
    SUCC: scitex-agent-container started

Two faults, no screen. So the second half of this file asserts the other
direction: the full pane is written to a FILE, and the single loud line names
that file — including when the write fails.

Real callables (a scripted ``capture_fn`` + recording ``send_keys_fn``), no
mocks — same fakes as ``test_tui_session_clear_compose.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._pane_context_log import (
    NO_PANE_CAPTURED,
    SNAPSHOT_SUBDIR,
    PaneSnapshot,
    log_pane_context,
    log_pane_fault,
    pane_tail,
    snapshot_path_for,
    write_pane_snapshot,
)
from scitex_agent_container.runtimes._tui_compose import (
    _COMPOSE_CLEAR_KEYS,
    clear_compose_buffer,
)

#: A line that appears ONLY in the pane, never in any fault message — so an
#: assertion about "where did the pane content go" cannot be satisfied by the
#: fault text happening to mention something similar.
_PANE_MARKER = "Running scheduled task (Jul 23 1:25am)"

_STALE_WITH_MARKER = (
    f"✻ {_PANE_MARKER}\n"
    "❯\xa0/compact\n"
    "  Context ████░░░░░░ 36% │ Usage ██░░░░░░░░ 20% (44m / 5h)\n"
)

#: The fault the compose-clear path reports on exhaustion — a real condition
#: that must STAY loud.
_FAULT_MARKER = "did NOT clear"


@dataclass
class _RecordingSend:
    keys: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.keys.append(key)


class _NeverClears:
    """``capture_fn`` whose live compose box always still holds stale text."""

    def __init__(self, pane: str = _STALE_WITH_MARKER) -> None:
        self._pane = pane

    def __call__(self, _name: str) -> str:
        return self._pane


def _no_sleep(_s: float) -> None:
    return None


def _exhaust_the_clear(caplog, sender: _RecordingSend) -> None:
    """Drive ``clear_compose_buffer`` to its give-up path with DEBUG capture."""
    with caplog.at_level(logging.DEBUG):
        clear_compose_buffer(
            "grant",
            capture_fn=_NeverClears(),
            send_keys_fn=sender,
            max_attempts=2,
            poll_s=0.0,
            sleep_fn=_no_sleep,
        )


def _records_at(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


# ---------------------------------------------------------------------------
# The pane transcription moves off the ERROR level
# ---------------------------------------------------------------------------


def test_pane_content_is_not_logged_at_error_level(caplog) -> None:
    # Arrange
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    # Assert — no ERROR record carries the other session's screen.
    assert not any(_PANE_MARKER in m for m in _records_at(caplog, logging.ERROR))


def test_pane_content_is_logged_at_info_level(caplog) -> None:
    # Arrange — the evidence must still be REACHABLE, just not shouted.
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    # Assert
    assert any(_PANE_MARKER in m for m in _records_at(caplog, logging.INFO))


def test_the_fault_itself_stays_at_error_level(caplog) -> None:
    # Arrange — the compose buffer really did not clear; that is a genuine
    # condition and demoting IT would hide a real fault.
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    # Assert
    assert any(_FAULT_MARKER in m for m in _records_at(caplog, logging.ERROR))


def test_the_error_record_is_a_single_line(caplog) -> None:
    # Arrange — one ERROR line == one fault. The multi-line record is exactly
    # what let the formatter stamp ERRO: onto 14 lines of someone else's UI.
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    fault = [m for m in _records_at(caplog, logging.ERROR) if _FAULT_MARKER in m]
    # Assert
    assert "\n" not in fault[0]


def test_a_gesture_was_actually_sent(caplog) -> None:
    # Arrange — guards the harness itself: if the exhaustion path never ran,
    # every "not at ERROR" assertion above would pass vacuously.
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    # Assert
    assert sender.keys == list(_COMPOSE_CLEAR_KEYS) * 2


# ---------------------------------------------------------------------------
# The helper in isolation
# ---------------------------------------------------------------------------


def test_log_pane_context_emits_info_not_error(caplog) -> None:
    # Arrange
    log = logging.getLogger("test-pane-context")
    # Act
    with caplog.at_level(logging.DEBUG, logger="test-pane-context"):
        log_pane_context(log, "grant", _STALE_WITH_MARKER)
    # Assert
    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_log_pane_context_says_so_when_nothing_was_captured(caplog) -> None:
    # Arrange — an empty pane is evidence too; a blank record would read as
    # "nothing was wrong here".
    log = logging.getLogger("test-pane-context")
    # Act
    with caplog.at_level(logging.DEBUG, logger="test-pane-context"):
        log_pane_context(log, "grant", "")
    # Assert
    assert NO_PANE_CAPTURED in caplog.records[0].getMessage()


def test_pane_tail_keeps_only_the_last_rows() -> None:
    # Arrange
    pane = "\n".join(f"row{i}" for i in range(30))
    # Act
    tail = pane_tail(pane, lines=3)
    # Assert
    assert tail == "row27\nrow28\nrow29"


# ---------------------------------------------------------------------------
# ...and the evidence SURVIVES that demotion: a durable snapshot, named loudly
# ---------------------------------------------------------------------------

#: A pane taller than the 14-row tail, so "the tail carried it" can never be
#: mistaken for "the snapshot carried it". Row 0 is reachable ONLY from the file.
_TALL_PANE = "\n".join(f"row{i}" for i in range(40))
_ONLY_IN_THE_FULL_PANE = "row0"

#: What the loud line says when the snapshot could not be written.
_NOT_SAVED = "NOT SAVED"


def _fault(log, name, pane, *, root=None) -> PaneSnapshot:
    """Drive one fault through the helper every real call site now uses."""
    return log_pane_fault(log, name, pane, "boot fault for %s", name, root=root)


@pytest.fixture
def tall_pane_snapshot(tmp_path) -> PaneSnapshot:
    """One fault reported against ``_TALL_PANE``, written under ``tmp_path``."""
    log = logging.getLogger("test-pane-snapshot")
    return _fault(log, "grant", _TALL_PANE, root=tmp_path)


@pytest.fixture
def blocked_root(tmp_path) -> Path:
    """A root whose snapshot dir CANNOT be created — a file occupies its path."""
    (tmp_path / SNAPSHOT_SUBDIR).write_text("not a directory", encoding="utf-8")
    return tmp_path


def test_the_fault_line_names_the_snapshot_file(caplog) -> None:
    # Arrange — the operator reads a console that shows ERRO and drops INFO, so
    # the loud line must be self-sufficient about where the screen went.
    sender = _RecordingSend()
    # Act
    _exhaust_the_clear(caplog, sender)
    # Assert
    expected = str(snapshot_path_for("grant"))
    assert any(expected in m for m in _records_at(caplog, logging.ERROR))


def test_a_reported_fault_writes_its_snapshot(tall_pane_snapshot) -> None:
    # Arrange — the fixture already reported one fault.
    # Act
    path = tall_pane_snapshot.path
    # Assert
    assert path is not None and path.is_file()


def test_the_snapshot_holds_rows_the_tail_would_have_dropped(
    tall_pane_snapshot,
) -> None:
    # Arrange — 40 rows; the INFO tail shows only the last 14.
    # Act
    written = tall_pane_snapshot.path.read_text(encoding="utf-8")
    # Assert
    assert _ONLY_IN_THE_FULL_PANE in written


def test_the_tail_alone_would_have_dropped_those_rows() -> None:
    # Arrange — guards the test above: if the tail happened to include row0,
    # its assertion would pass without the snapshot doing any work.
    pane = _TALL_PANE
    # Act
    tail = pane_tail(pane)
    # Assert
    assert _ONLY_IN_THE_FULL_PANE not in tail


def test_the_snapshot_lands_under_the_runtime_root(tmp_path) -> None:
    # Arrange
    session = "tui-grant"
    # Act
    path = snapshot_path_for(session, root=tmp_path)
    # Assert
    assert path == tmp_path / SNAPSHOT_SUBDIR / "tui-grant.log"


def test_a_second_fault_keeps_the_first_one(tmp_path) -> None:
    # Arrange — a boot usually trips more than one fault and the SEQUENCE is
    # the diagnosis, so an overwrite would leave only the last one.
    log = logging.getLogger("test-pane-snapshot-append")
    _fault(log, "grant", "first pane\n", root=tmp_path)
    # Act
    snapshot = _fault(log, "grant", "second pane\n", root=tmp_path)
    # Assert
    assert "first pane" in snapshot.path.read_text(encoding="utf-8")


def test_a_second_fault_also_records_itself(tmp_path) -> None:
    # Arrange
    log = logging.getLogger("test-pane-snapshot-append")
    _fault(log, "grant", "first pane\n", root=tmp_path)
    # Act
    snapshot = _fault(log, "grant", "second pane\n", root=tmp_path)
    # Assert
    assert "second pane" in snapshot.path.read_text(encoding="utf-8")


def test_an_empty_pane_is_still_recorded_as_evidence(tmp_path) -> None:
    # Arrange — a session that already died leaves nothing to capture, and THAT
    # is the finding. A zero-byte record would read as "nothing was wrong here".
    log = logging.getLogger("test-pane-snapshot-empty")
    # Act
    snapshot = _fault(log, "grant", "", root=tmp_path)
    # Assert
    assert NO_PANE_CAPTURED in snapshot.path.read_text(encoding="utf-8")


def test_a_failed_write_is_reported_as_unsaved(blocked_root) -> None:
    # Arrange — silently skipping would be the worst outcome: the absent
    # snapshot would read as "there was nothing to see".
    log = logging.getLogger("test-pane-snapshot-failed")
    # Act
    snapshot = _fault(log, "grant", _TALL_PANE, root=blocked_root)
    # Assert
    assert snapshot.path is None


def test_a_failed_write_says_why(blocked_root) -> None:
    # Arrange
    log = logging.getLogger("test-pane-snapshot-failed")
    # Act
    snapshot = _fault(log, "grant", _TALL_PANE, root=blocked_root)
    # Assert
    assert snapshot.error


def test_a_failed_write_names_the_target_on_the_loud_line(blocked_root, caplog) -> None:
    # Arrange — the reader must still learn WHERE it was supposed to land.
    log = logging.getLogger("test-pane-snapshot-failed-loud")
    # Act
    with caplog.at_level(logging.DEBUG, logger="test-pane-snapshot-failed-loud"):
        snapshot = _fault(log, "grant", _TALL_PANE, root=blocked_root)
    # Assert
    assert any(
        _NOT_SAVED in m and str(snapshot.target) in m
        for m in _records_at(caplog, logging.ERROR)
    )


def test_a_failed_write_never_raises_over_the_fault_it_reports(
    blocked_root,
) -> None:
    # Arrange — losing the evidence must not also lose the fault.
    pane = _TALL_PANE
    # Act
    snapshot = write_pane_snapshot("grant", pane, root=blocked_root)
    # Assert
    assert isinstance(snapshot, PaneSnapshot)


def test_the_snapshot_filename_cannot_climb_out_of_its_directory(tmp_path) -> None:
    # Arrange — the session name becomes a filename.
    session = "../../escape"
    # Act
    path = snapshot_path_for(session, root=tmp_path)
    # Assert
    assert path.parent == tmp_path / SNAPSHOT_SUBDIR


def test_the_loud_line_still_carries_no_pane_content(tmp_path, caplog) -> None:
    # Arrange — the 2026-07-23 fix must not regress: the ERROR record names the
    # file, it does not paste the screen back into it.
    log = logging.getLogger("test-pane-snapshot-quiet")
    # Act
    with caplog.at_level(logging.DEBUG, logger="test-pane-snapshot-quiet"):
        _fault(log, "grant", _TALL_PANE, root=tmp_path)
    # Assert
    assert not any(
        _ONLY_IN_THE_FULL_PANE in m for m in _records_at(caplog, logging.ERROR)
    )
