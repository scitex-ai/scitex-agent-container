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

Found because it turned CI red: two ``test_lifecycle`` tests assert this file
exists, and they only ever passed when some OTHER test in the same xdist worker
happened to create ``<floor>/runtime/alpha/`` first. Reordering the suite (two
new test files) exposed a latent order-dependency AND the real hole under it.

NO MOCKS (repo doctrine): the exploding tmux layer below is a REAL callable
passed through the production seam, not a patched internal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._start_failure_diag import raise_start_failure

_RUNTIME_DIR_ENV = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"


class _Cfg:
    """A real minimal config — the attributes the diag path resolution reads."""

    def __init__(self, name: str = "alpha", runtime: str = "apptainer") -> None:
        self.name = name
        self.runtime = runtime


def _wedged_tmux(_config: Any) -> str:
    """A REAL capture collaborator that fails the way a wedged tmux server does."""
    raise OSError("tmux server is wedged; cannot capture pane")


@pytest.fixture
def state_root(tmp_path: Path) -> Iterator[Path]:
    """Point sac's runtime root at a dir the agent has NEVER written to.

    The real production shape: an agent whose start failed early has no state
    dir, because nothing ever got far enough to create one.
    """
    root = tmp_path / "runtime"
    previous = os.environ.get(_RUNTIME_DIR_ENV)
    os.environ[_RUNTIME_DIR_ENV] = str(root)
    yield root
    if previous is None:
        os.environ.pop(_RUNTIME_DIR_ENV, None)
    else:
        os.environ[_RUNTIME_DIR_ENV] = previous


def _diag_log(config: _Cfg) -> Path:
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    return state_dir_for_config(config) / "start_failure_diag.log"


def _raise_and_swallow(config: _Cfg, **kwargs: Any) -> None:
    """Drive the real failure path. It ALWAYS raises; the record is the subject."""
    try:
        raise_start_failure(config, **kwargs)
    except RuntimeError:
        pass


def test_the_state_dir_really_is_absent(state_root: Path):
    """Guard the guard: if the dir already existed, every test below proves nothing."""
    # Arrange
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    config = _Cfg()
    # Act
    exists = state_dir_for_config(config).exists()
    # Assert
    assert exists is False


def test_the_diag_is_persisted_even_when_the_state_dir_does_not_exist(
    state_root: Path,
):
    """THE REGRESSION. Before the fix this record was silently discarded.

    The agents this runs for are exactly the ones whose start FAILED — and a
    start that failed early never created its state dir.
    """
    # Arrange
    config = _Cfg()
    # Act
    _raise_and_swallow(config)
    # Assert
    assert _diag_log(config).is_file()


def test_the_persisted_diag_names_the_real_reason(state_root: Path):
    """The record must state WHY, not merely exist."""
    # Arrange
    config = _Cfg()
    # Act
    _raise_and_swallow(config)
    # Assert
    assert "runtime.start() returned False" in _diag_log(config).read_text()


def test_a_wedged_tmux_does_not_destroy_the_persisted_record(state_root: Path):
    """The BEST-EFFORT half must never be able to kill the MANDATORY half.

    An apptainer agent has no tmux pane at all, and a wedged server throws.
    Neither is a reason to lose the only durable evidence of the failure.
    """
    # Arrange
    config = _Cfg()
    # Act
    _raise_and_swallow(config, capture_fn=_wedged_tmux)
    # Assert
    assert _diag_log(config).is_file()


def test_a_wedged_tmux_still_leaves_the_reason_in_the_record(state_root: Path):
    """Losing the pane tail is acceptable. Losing the CAUSE is not."""
    # Arrange
    config = _Cfg()
    # Act
    _raise_and_swallow(config, capture_fn=_wedged_tmux)
    # Assert
    assert "runtime.start() returned False" in _diag_log(config).read_text()


def test_the_raised_error_still_carries_the_cause_when_the_capture_fails(
    state_root: Path,
):
    """The loud failure stays loud even when diagnostics degrade."""
    # Arrange
    config = _Cfg()
    # Act
    # (the raise IS the act under test — it must still name the cause.)
    # Assert
    with pytest.raises(RuntimeError, match=r"runtime\.start\(\) returned False"):
        raise_start_failure(config, capture_fn=_wedged_tmux)
