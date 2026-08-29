"""A liveness probe that could not RUN must not report the pane as alive.

`_verify_tui` asked `_runtime_reports_running`, which returned True BOTH when
the runtime reported alive and when the probe raised — then returned
VERIFIED_UP with the words "the tmux pane process is alive". So whenever the
probe raised, sac asserted an observation that never happened, on the path 109
of 122 fleet specs take (measured 2026-08-20: tui 109, claude-agent-sdk 11,
apptainer 2). The success value doubled as the didn't-check value.

The boolean could not express the difference: refusing to convict on a raising
probe (correct, and what the DEAD arm needs) and refusing to acquit on one
(also correct, and what this arm needs) are opposite requirements on one value.
Hence three values.

The controls carry the weight: a fix that returned UNVERIFIED for everything
would satisfy the defect test and break every start in the fleet.

Separate file — `test__launch_verify.py` is 483 lines against a 512 cap.
PA-306: no mocks; hand-written runtime doubles matching the seam shape the
existing suite already uses.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._launch_verify import (
    PROBE_DEAD,
    PROBE_RUNNING,
    PROBE_UNKNOWN,
    UNVERIFIED,
    VERIFIED_FAILED,
    VERIFIED_UP,
    _runtime_liveness,
    _runtime_reports_running,
    _verify_tui,
)


class _AliveRuntime:
    def is_running(self, config) -> bool:  # noqa: ARG002 - runtime seam shape
        return True


class _DeadRuntime:
    def is_running(self, config) -> bool:  # noqa: ARG002 - runtime seam shape
        return False


class _RaisingRuntime:
    """A probe that cannot RUN — neither alive nor dead is observed."""

    def is_running(self, config):  # noqa: ARG002 - runtime seam shape
        raise OSError("tmux server not reachable")


def _config(name: str = "probe-x") -> SimpleNamespace:
    return SimpleNamespace(name=name)


@pytest.fixture
def clean_window() -> Iterator[None]:
    """Unset the real env var and restore it — no fixture rewrites internals."""
    # Arrange
    key = "SAC_VERIFY_WINDOW_S"
    saved = os.environ.pop(key, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[key] = saved


def _verdict(runtime, tmp_path: Path):
    """Exercise `_verify_tui` DIRECTLY — the function that changed.

    Going through `verify_launch` would not reach it: dispatch is
    `isinstance(runtime, TuiSessionRuntime)`, so a hand-written double
    falls to the generic heartbeat path instead. That path ALSO returns
    UNVERIFIED when no beat arrives, which would have made these tests
    pass without touching the code under test — green for a reason
    unrelated to the fix. Calling the function directly is what makes
    the assertions mean what they say.
    """
    return _verify_tui(
        _config(),
        runtime,
        tmp_path,
        time.time(),
        time.time,
    )


# ---------------------------------------------------------------------------
# The tri-state itself
# ---------------------------------------------------------------------------


def test_a_raising_probe_reads_unknown(clean_window) -> None:
    # Arrange
    runtime = _RaisingRuntime()
    # Act
    reading = _runtime_liveness(runtime, _config())
    # Assert
    assert reading == PROBE_UNKNOWN


def test_a_live_probe_reads_running(clean_window) -> None:
    # Arrange
    runtime = _AliveRuntime()
    # Act
    reading = _runtime_liveness(runtime, _config())
    # Assert
    assert reading == PROBE_RUNNING


def test_a_false_probe_reads_dead(clean_window) -> None:
    # Arrange
    runtime = _DeadRuntime()
    # Act
    reading = _runtime_liveness(runtime, _config())
    # Assert
    assert reading == PROBE_DEAD


# ---------------------------------------------------------------------------
# The defect: an unobserved pane was reported alive
# ---------------------------------------------------------------------------


def test_a_raising_probe_does_not_report_the_pane_alive(
    clean_window, tmp_path: Path
) -> None:
    # Arrange
    runtime = _RaisingRuntime()
    # Act
    verdict = _verdict(runtime, tmp_path)
    # Assert — returned VERIFIED_UP before the fix
    assert verdict.status != VERIFIED_UP, verdict.evidence


def test_a_raising_probe_says_it_cannot_verify(clean_window, tmp_path: Path) -> None:
    # Arrange
    runtime = _RaisingRuntime()
    # Act
    verdict = _verdict(runtime, tmp_path)
    # Assert
    assert verdict.status == UNVERIFIED, verdict.evidence


def test_a_raising_probe_is_not_reported_as_death_either(
    clean_window, tmp_path: Path
) -> None:
    # Arrange — the false-RED lesson: unknown must not convict
    runtime = _RaisingRuntime()
    # Act
    verdict = _verdict(runtime, tmp_path)
    # Assert
    assert verdict.status != VERIFIED_FAILED, verdict.evidence


# ---------------------------------------------------------------------------
# Controls — the fleet must keep starting
# ---------------------------------------------------------------------------


def test_a_live_runtime_still_verifies_up(clean_window, tmp_path: Path) -> None:
    # Arrange — the arm that carries every healthy start
    runtime = _AliveRuntime()
    # Act
    verdict = _verdict(runtime, tmp_path)
    # Assert
    assert verdict.status == VERIFIED_UP, verdict.evidence


def test_a_dead_runtime_still_verifies_failed(clean_window, tmp_path: Path) -> None:
    # Arrange — the arm that catches a stillborn container
    runtime = _DeadRuntime()
    # Act
    verdict = _verdict(runtime, tmp_path)
    # Assert
    assert verdict.status == VERIFIED_FAILED, verdict.evidence


def test_the_boolean_shim_still_declines_to_convict_on_unknown(clean_window) -> None:
    # Arrange — the DEAD arm reads this and must behave exactly as before
    runtime = _RaisingRuntime()
    # Act
    reports_running = _runtime_reports_running(runtime, _config())
    # Assert — only a definite DEAD is falsey
    assert reports_running is True
