"""A detached launch that exits 0 having started NOTHING must not be silent.

THE MEASURED INCIDENT (2026-09-06, scitex-compute-04) THIS PINS
---------------------------------------------------------------
A container ran ``sac agents start dotfiles``. The host's ``sac listen``
answered ``202 accepted (phase=launch)`` at 07:23:13 and the CLI exited 0 with
"launch verification skipped: start was brokered to the host, the evidence lives
on the host". There was no evidence on the host. No ``tui-dotfiles`` tmux
session, no ``STARTUP_FAILED`` marker, not one file written under the agent's
state dir that day, and — for the following six minutes — not one application
line in the ``sac-listen`` journal beyond the 202 itself and the caller's own
status polls. The service was healthy throughout. The SAME command run on the
host worked first try and self-verified.

Host forensics established the shape: the credential boot gate was taken at
07:22:43 (its lock file holds the daemon pid, stamped exactly
``AGENT_START_DEADLINE_S`` before the 202) and was released normally, so the
launch RAN TO COMPLETION and the detach done-callback DID fire. It simply took
the branch that writes nothing, because the child exited 0.

WHAT IS UNDER TEST HERE — THE SILENCE, NOT THE CHILD
-----------------------------------------------------
WHY the child exited 0 without starting anything is NOT established, and cannot
be recovered from that incident precisely because of the defect below: the
child's rc, stdout and stderr existed in the daemon's memory and were read by
nobody. So this module does not test a theory about the child. It tests the
recording layer that lost it:

* the synchronous ``POST /agents`` path does NOT believe ``rc == 0`` — it runs
  the post-ack liveness probe and, on a positively-observed absence, writes a
  ``STARTUP_FAILED`` marker and answers 502;
* once the handler answers 202 that probe is unreachable, and the detached path
  treated ``rc == 0`` as success and wrote nothing;
* so the 202 was a DEGRADED MODE, contrary to ``_spawn_detach``'s own docstring
  claim that "the diagnostic is identical either way".

The two assertions are therefore: a marker appears, and a log line appears. A
failure that leaves neither is indistinguishable from an agent nobody asked to
start, which is exactly how six minutes of a real launch went missing.

Same convention as the sibling deadline/subprocess modules: a real
``TestClient`` against a real fake ``sac`` shim on ``$PATH``. No mocks — the
shim really outlives the deadline and really exits 0 without creating a
session, so this exercises the actual asyncio shield/detach/probe path.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._startup_failed import read_marker
from scitex_agent_container._listen import _handler_deadline, _spawn_detach
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._runners._session_state import state_dir_for
from scitex_agent_container._state import registry as _reg

_TOKEN = "spawn-detach-rc0-token"

# The shim outlives the handler's deadline so the 202/detach path is the one
# taken; it then exits 0 WITHOUT creating any session — the incident's shape.
_SHIM_SLEEP_S = 1.0
_TEST_DEADLINE_S = 0.3

# The post-ack grace window for this module. Positive (so the probe actually
# RUNS — the sibling deadline tests set it to 0 to opt out) and short, because
# the shim writes no pidfile and never will, so the probe's answer is already
# determined; the window only decides how long we wait to hear it.
_PROBE_WINDOW_S = "0.5"

# FAILSAFE ONLY — never a pacing budget. The aftermath loop exits the INSTANT
# its condition holds, so a generous ceiling costs a fast run nothing and costs
# a loaded run its false failure. See the sibling module's note on the
# 2026-08-12 red: a clock standing in for an event.
_AFTERMATH_TIMEOUT_S = 60.0


class _RecordCollector(logging.Handler):
    """Collect ``_spawn_detach``'s own records, whichever thread emits them.

    Attached DIRECTLY to the module's logger rather than using ``caplog``: the
    records are emitted from the app's event-loop thread during a fixture, and
    a handler on the logger itself is indifferent to both — no phase bookkeeping
    and no dependence on the root logger's level, which is the thing that makes
    this path invisible in production in the first place.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _install_rc0_no_session_shim(bin_dir: Path, done_flag: Path) -> None:
    """The incident's child: outlives the deadline, exits 0, starts NOTHING.

    It writes no ``apptainer_pid``, opens no tmux session and leaves no trace in
    the agent's state dir — the only thing it does is touch ``done_flag`` so the
    test can prove the launch really ran to completion rather than being
    cancelled by the 202.
    """
    script = bin_dir / "sac"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, pathlib\n"
        f"time.sleep({_SHIM_SLEEP_S})\n"
        f"pathlib.Path({str(done_flag)!r}).write_text('done')\n"
        'sys.stdout.write("started agent (nothing was actually started)\\n")\n'
        "sys.exit(0)\n"
    )
    script.chmod(0o755)


class _Rc0Spawn(NamedTuple):
    status_code: int
    launch_completed: bool
    marker: dict | None
    log_messages: list[str]


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors the sibling tests)."""
    saved = {
        key: os.environ.get(key)
        for key in ("SCITEX_AGENT_CONTAINER_STATE_DB", "HOME")
    }
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(tmp_path / "state.db")
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def short_deadline():
    """Shrink the handler's answer-by budget so the 202/detach path is taken.

    ``agents_start`` imports ``AGENT_START_DEADLINE_S`` INSIDE its body, so
    reassigning the module attribute is picked up on the next call — the same
    save/restore seam the sibling deadline module uses.
    """
    saved = _handler_deadline.AGENT_START_DEADLINE_S
    _handler_deadline.AGENT_START_DEADLINE_S = _TEST_DEADLINE_S
    try:
        yield _TEST_DEADLINE_S
    finally:
        _handler_deadline.AGENT_START_DEADLINE_S = saved


@pytest.fixture
def captured_detach_logs():
    """Collect ``_spawn_detach``'s records for the duration of one test."""
    collector = _RecordCollector()
    detach_logger = logging.getLogger(_spawn_detach.__name__)
    saved_level = detach_logger.level
    detach_logger.addHandler(collector)
    detach_logger.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        detach_logger.removeHandler(collector)
        detach_logger.setLevel(saved_level)


@pytest.fixture
def rc0_no_session_spawn(
    isolated_listen_env,
    short_deadline,
    captured_detach_logs,
    env_save_restore,
    tmp_path: Path,
) -> _Rc0Spawn:
    """One brokered launch that 202s, then exits 0 having started nothing.

    Function-scoped, like the sibling deadline module's fixtures: a
    module-scoped fixture that mutates state in its body is what STX-TQ004
    forbids, and the reason it forbids it — one test's leftovers deciding
    another's verdict — is exactly the failure mode a regression test for a
    SILENT bug can least afford, because its evidence is an ABSENCE.
    """
    name = "rc0-no-session"
    flag = tmp_path / "flag_rc0"
    bin_dir = tmp_path / "sac_bin_rc0"
    bin_dir.mkdir()
    _install_rc0_no_session_shim(bin_dir, flag)
    env_save_restore.set(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    # Positive, unlike the sibling deadline module: the probe must RUN.
    env_save_restore.set(
        "SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", _PROBE_WINDOW_S
    )
    with TestClient(create_app(token=_TOKEN)) as client:
        resp = client.post(
            "/agents",
            json={"name": name},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        # Stay inside the client context so the app's event loop is alive and
        # the detached done-callback + verification task can actually run.
        watch_until = time.monotonic() + _AFTERMATH_TIMEOUT_S
        marker = None
        while time.monotonic() < watch_until:
            marker = read_marker(state_dir_for(name))
            if flag.exists() and marker is not None:
                break
            time.sleep(0.1)
    return _Rc0Spawn(
        status_code=resp.status_code,
        launch_completed=flag.exists(),
        marker=marker,
        log_messages=list(captured_detach_logs.messages),
    )


# ---------------------------------------------------------------------------
# Preconditions — this really is the detached, uncancelled, rc=0 path
# ---------------------------------------------------------------------------


def test_the_slow_launch_is_answered_with_202(rc0_no_session_spawn) -> None:
    """Guard on the setup: a 200 here would mean the probe ran synchronously."""
    # Arrange
    result = rc0_no_session_spawn
    # Act
    status = result.status_code
    # Assert
    assert status == 202


def test_the_202_does_not_cancel_the_launch(rc0_no_session_spawn) -> None:
    """The shim reached its final act, so the child really did exit 0."""
    # Arrange
    result = rc0_no_session_spawn
    # Act
    completed = result.launch_completed
    # Assert
    assert completed is True


# ---------------------------------------------------------------------------
# THE REGRESSION: the silence itself
# ---------------------------------------------------------------------------


def test_an_rc0_launch_that_started_nothing_writes_a_startup_failed_marker(
    rc0_no_session_spawn,
) -> None:
    """The incident, exactly: rc=0, no session, and nothing written anywhere.

    Before the fix this assertion fails with ``marker is None`` — which is not
    the test being flaky, it IS the bug: ``_on_launch_done`` wrote a marker only
    when ``returncode not in (0, None)``, so a child that exited 0 having
    started nothing was recorded nowhere, and ``GET /agents/<name>/status`` —
    the route the 202 tells the caller to poll — had nothing to report.
    """
    # Arrange
    result = rc0_no_session_spawn
    # Act
    marker = result.marker
    # Assert
    assert marker is not None


def test_the_late_marker_names_the_post_ack_liveness_phase(
    rc0_no_session_spawn,
) -> None:
    """Same phase the SYNCHRONOUS path stamps — the 202 is not a degraded mode.

    If the detached path convicted under some other phase, a caller could tell
    which side of the deadline its spawn fell on, and the equivalence the module
    docstring promises would be decorative.
    """
    # Arrange
    result = rc0_no_session_spawn
    # Act
    phase = (result.marker or {}).get("phase")
    # Assert
    assert phase == "post_ack_liveness"


def test_the_late_marker_carries_the_probe_s_positively_observed_kind(
    rc0_no_session_spawn,
) -> None:
    """A ``post_ack_*`` kind is what makes the marker actionable rather than
    merely present — it says the probe RAN and observed an absence."""
    # Arrange
    result = rc0_no_session_spawn
    # Act
    kind = str((result.marker or {}).get("kind", ""))
    # Assert
    assert kind.startswith("post_ack_")


def test_the_detached_outcome_is_logged(rc0_no_session_spawn) -> None:
    """The other half of the silence: the journal held ZERO application lines.

    ``rg -n 'logging|logger|print\\('`` over the whole path returned nothing
    before this change, so a launch could complete, fail, or vanish without the
    daemon ever saying a word. The 202 in the journal was uvicorn's ACCESS line,
    not sac's.
    """
    # Arrange
    result = rc0_no_session_spawn
    # Act
    messages = result.log_messages
    # Assert
    assert messages != []


def test_a_logged_line_names_the_agent_that_started_nothing(
    rc0_no_session_spawn,
) -> None:
    """An operator reading the journal must be able to tell WHICH agent."""
    # Arrange
    result = rc0_no_session_spawn
    # Act
    naming = [m for m in result.log_messages if "rc0-no-session" in m]
    # Assert
    assert naming != []


def test_a_logged_line_says_the_launch_started_nothing(
    rc0_no_session_spawn,
) -> None:
    """The line has to carry the VERDICT, not just the agent's name.

    A log that says only "launch finished rc=0" reproduces the original lie in
    a louder voice; the point is that the daemon now says the rc=0 exit did not
    produce a running agent.
    """
    # Arrange
    result = rc0_no_session_spawn
    # Act
    verdicts = [m for m in result.log_messages if "STARTED NOTHING" in m]
    # Assert
    assert verdicts != []
