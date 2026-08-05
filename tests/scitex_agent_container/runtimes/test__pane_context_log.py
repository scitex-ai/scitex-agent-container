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

Real callables (a scripted ``capture_fn`` + recording ``send_keys_fn``), no
mocks — same fakes as ``test_tui_session_clear_compose.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scitex_agent_container.runtimes._pane_context_log import (
    NO_PANE_CAPTURED,
    log_pane_context,
    pane_tail,
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
