"""The start-failure diagnostic must SURVIVE — that is the entire job.

WHY THIS FILE EXISTS (2026-07-14). ``raise_start_failure`` persists
``<state>/start_failure_diag.log`` because a false-negative start leaves NO
REGISTRY ROW, so killing the tmux session by hand is often the only way to stop
the agent — and that destroys the live pane capture forever. This file was the
copy that was supposed to survive.

It did not survive. The MANDATORY write was the last statement inside the
BEST-EFFORT pane-capture ``try``, under a bare ``except Exception``. So any
earlier hiccup threw, got swallowed, and the record was silently discarded. The
commonest hiccup is the plainest one: **the state dir does not exist yet**,
because a start that failed early never created it — ``write_text`` into a
missing directory raises ``FileNotFoundError``. Precisely when the diagnostic
mattered most, it was thrown away.

The caller was then handed ``(no pane diagnostics available)`` — a message
blaming the PANE CAPTURE for a failure it never observed. Exactly the disease the
liveness verdict next door exists to kill: an error asserting a cause it did not
see.

EVERY TEST HERE OWNS A UNIQUE AGENT NAME, and that is not cosmetic. The bug
surfaced because two ``test_lifecycle`` tests assert this file for an agent
called ``alpha``, and they only ever passed when some OTHER test in the same
xdist worker happened to create ``<floor>/runtime/alpha/`` first. A suite that
depends on a sibling's leftovers is testing the schedule, not the code — so these
tests refuse to share a name with anyone.

NO MOCKS (repo doctrine): the exploding tmux layer below is a REAL callable
passed through the production seam, not a patched internal.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._lifecycle._start_failure_diag import raise_start_failure


class _Cfg:
    """A real minimal config — the attributes the diag path resolution reads."""

    def __init__(self, name: str, runtime: str = "apptainer") -> None:
        self.name = name
        self.runtime = runtime


def _wedged_tmux(_config: Any) -> str:
    """A REAL capture collaborator that fails the way a wedged tmux server does."""
    raise OSError("tmux server is wedged; cannot capture pane")


@pytest.fixture
def config() -> _Cfg:
    """An agent NOBODY else has ever started, so its state dir cannot exist.

    That is the real production shape: a start that failed early never created
    its state dir. Sharing a name with another test would silently hand us a
    directory somebody else made, and the regression below would stop being
    reproducible — which is exactly how the bug survived.
    """
    return _Cfg(name=f"diagfail-{uuid.uuid4().hex[:12]}")


def _state_dir(config: _Cfg) -> Path:
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    return state_dir_for_config(config)


def _diag_log(config: _Cfg) -> Path:
    return _state_dir(config) / "start_failure_diag.log"


def _raise_and_swallow(config: _Cfg, **kwargs: Any) -> None:
    """Drive the real failure path. It ALWAYS raises; the RECORD is the subject."""
    try:
        raise_start_failure(config, **kwargs)
    except RuntimeError:
        pass


def test_the_state_dir_really_is_absent(config: _Cfg):
    """Guard the guard: if the dir already existed, every test below proves nothing."""
    # Arrange
    state_dir = _state_dir(config)
    # Act
    exists = state_dir.exists()
    # Assert
    assert exists is False


def test_the_diag_is_persisted_even_when_the_state_dir_does_not_exist(config: _Cfg):
    """THE REGRESSION. Before the fix this record was silently discarded.

    The agents this runs for are exactly the ones whose start FAILED — and a
    start that failed early never created its state dir.
    """
    # Arrange
    log = _diag_log(config)
    # Act
    _raise_and_swallow(config)
    # Assert
    assert log.is_file()


def test_the_persisted_diag_names_the_real_reason(config: _Cfg):
    """The record must state WHY, not merely exist."""
    # Arrange
    log = _diag_log(config)
    # Act
    _raise_and_swallow(config)
    # Assert
    assert "runtime.start() returned False" in log.read_text()


def test_a_wedged_tmux_does_not_destroy_the_persisted_record(config: _Cfg):
    """The BEST-EFFORT half must never be able to kill the MANDATORY half.

    An apptainer agent has no tmux pane at all, and a wedged server throws.
    Neither is a reason to lose the only durable evidence of the failure.
    """
    # Arrange
    log = _diag_log(config)
    # Act
    _raise_and_swallow(config, capture_fn=_wedged_tmux)
    # Assert
    assert log.is_file()


def test_a_wedged_tmux_still_leaves_the_reason_in_the_record(config: _Cfg):
    """Losing the pane tail is acceptable. Losing the CAUSE is not."""
    # Arrange
    log = _diag_log(config)
    # Act
    _raise_and_swallow(config, capture_fn=_wedged_tmux)
    # Assert
    assert "runtime.start() returned False" in log.read_text()


def test_the_raised_error_still_carries_the_cause_when_the_capture_fails(config: _Cfg):
    """The loud failure stays loud even when diagnostics degrade."""
    # Arrange
    capture = _wedged_tmux
    # Act
    # (the raise IS the act under test — it must still name the cause.)
    # Assert
    with pytest.raises(RuntimeError, match=r"runtime\.start\(\) returned False"):
        raise_start_failure(config, capture_fn=capture)
