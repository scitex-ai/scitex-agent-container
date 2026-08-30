"""``POST /agents`` must ANSWER within its declared deadline, not go silent.

The bug: the handler had no answer-by bound. Its wait is unbounded by
construction — the OAuth settle window is held INSIDE an exclusive flock
(``_credential_refresh_lock``), so under N concurrent spawns waiter N pays N-1
predecessors before its own launch even starts. The caller had no way to learn
it was QUEUED, because from outside a queue and a dead host produce the same
silence. Its socket timeout became the error channel, and a spawn that
SUCCEEDED was reported to the fleet as a failure — the standing "I can't start
agents" complaint. A retry on that mutating route starts a SECOND agent.

Same convention as ``test__agent_exec_declined.py`` / ``test__agent_exec_
subprocess.py``: a real ``TestClient`` against a real fake ``sac`` shim on
``$PATH``. No mocks — the slow shim really sleeps and the failing shim really
exits non-zero, so these exercise the actual asyncio timeout/shield path rather
than a stubbed stand-in for it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._startup_failed import read_marker
from scitex_agent_container._listen import _handler_deadline
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._runners._session_state import state_dir_for
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db

_TOKEN = "test-token-agent-exec-deadline"

# The shim sleeps this long; the handler's deadline is set well below it, so the
# handler MUST answer while the launch is still running.
_SHIM_SLEEP_S = 2.0
_TEST_DEADLINE_S = 0.3

# FAILSAFE ONLY — never a pacing budget. The aftermath loop below exits the
# INSTANT its condition holds, so a generous ceiling costs a fast run nothing
# and costs a loaded run its false failure. It used to be ``_SHIM_SLEEP_S * 3``
# (6 s), which is not a bound on anything the system promises: it has to cover a
# real fork+exec of a CPython shim, a 2 s sleep, the handler's detached
# done-callback and a marker write, all on a box CI deliberately saturates
# (`-n 32` on 32 cores, at `nice 19`). Measured 2026-08-12 on scitex-compute-04:
# ``test_a_failure_after_the_202_still_writes_its_marker`` went red under the
# full suite with ``marker is None`` — the marker was not missing, the loop had
# simply stopped looking — while the same test passes in isolation. A clock was
# standing in for the event; this is the event's failsafe.
_AFTERMATH_TIMEOUT_S = 60.0


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors the sibling tests)."""
    db = tmp_path / "state.db"
    saved_env_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default_db = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        state_db.DEFAULT_DB_PATH = saved_default_db
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_env_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env_db
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


@pytest.fixture
def short_deadline():
    """Shrink the handler's answer-by budget (save/restore seam).

    ``agents_start`` imports ``AGENT_START_DEADLINE_S`` INSIDE the function
    body, so reassigning the module attribute is picked up on the next call —
    the same save/restore-seam idiom the sibling tests use for
    ``state_db.DEFAULT_DB_PATH``.
    """
    saved = _handler_deadline.AGENT_START_DEADLINE_S
    _handler_deadline.AGENT_START_DEADLINE_S = _TEST_DEADLINE_S
    try:
        yield _TEST_DEADLINE_S
    finally:
        _handler_deadline.AGENT_START_DEADLINE_S = saved


def _install_slow_sac_shim(bin_dir: Path, *, exit_code: int, done_flag: Path) -> None:
    """A shim that outlives the handler's deadline, then finishes for real.

    Touches ``done_flag`` on the way out so a test can prove the launch was
    NOT cancelled by the 202 — the whole promise of the ``asyncio.shield``.
    """
    script = bin_dir / "sac"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, pathlib\n"
        f"time.sleep({_SHIM_SLEEP_S})\n"
        f"pathlib.Path({str(done_flag)!r}).write_text('done')\n"
        'sys.stderr.write("slow shim finished\\n")\n'
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)


def _install_fast_sac_shim(bin_dir: Path) -> None:
    """An immediate success — the deadline must NOT fire for this one."""
    script = bin_dir / "sac"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n")
    script.chmod(0o755)


def _prepare_env(env_save_restore, bin_dir: Path) -> None:
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Skip the post-ack liveness grace: this module is about the HANDLER's
    # deadline, and a 20s probe would dominate every timing here.
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")


def _post(client: TestClient, name: str):
    return client.post(
        "/agents",
        json={"name": name},
        headers={"authorization": f"Bearer {_TOKEN}"},
    )


class _SlowSpawn(NamedTuple):
    status_code: int
    body: dict
    elapsed_s: float
    launch_still_running_at_answer: bool
    launch_completed: bool
    marker: dict | None


def _run_slow_spawn(
    env_save_restore, tmp_path: Path, *, name: str, exit_code: int
) -> _SlowSpawn:
    """POST against a shim that outlives the deadline, then watch the aftermath.

    Returns the immediate response AND what happened after it, so each test can
    assert exactly one thing about a single expensive (~4s) run.
    """
    flag = tmp_path / f"flag_{name}"
    bin_dir = tmp_path / f"sac_bin_{name}"
    bin_dir.mkdir()
    _install_slow_sac_shim(bin_dir, exit_code=exit_code, done_flag=flag)
    _prepare_env(env_save_restore, bin_dir)
    with TestClient(create_app(token=_TOKEN)) as client:
        started = time.monotonic()
        resp = _post(client, name)
        elapsed = time.monotonic() - started
        # OBSERVE the race, do not time it. The shim touches ``flag`` as its
        # LAST act, so "the flag is still absent now" is a direct reading of
        # "the answer arrived before the launch finished" — the property this
        # module is about — taken at the only instant it is true or false.
        still_running_at_answer = not flag.exists()
        # Stay inside the client context so the app's event loop is alive and
        # the detached done-callback can actually run.
        watch_until = time.monotonic() + _AFTERMATH_TIMEOUT_S
        marker = None
        while time.monotonic() < watch_until:
            marker = read_marker(state_dir_for(name))
            if flag.exists() and (exit_code == 0 or marker is not None):
                break
            time.sleep(0.1)
    return _SlowSpawn(
        status_code=resp.status_code,
        body=resp.json(),
        elapsed_s=elapsed,
        launch_still_running_at_answer=still_running_at_answer,
        launch_completed=flag.exists(),
        marker=marker,
    )


@pytest.fixture
def slow_ok_spawn(isolated_listen_env, short_deadline, env_save_restore, tmp_path):
    """One slow SUCCEEDING spawn, shared by the assertions about it."""
    return _run_slow_spawn(env_save_restore, tmp_path, name="slow-ok", exit_code=0)


@pytest.fixture
def slow_failing_spawn(isolated_listen_env, short_deadline, env_save_restore, tmp_path):
    """One slow FAILING spawn — the late-failure diagnosability case."""
    return _run_slow_spawn(env_save_restore, tmp_path, name="slow-fail", exit_code=255)


# ---------------------------------------------------------------------------
# A launch that outlives the deadline is ACCEPTED, not failed and not silent
# ---------------------------------------------------------------------------


def test_a_launch_past_the_deadline_answers_202(slow_ok_spawn) -> None:
    # Arrange
    result = slow_ok_spawn
    # Act
    status = result.status_code
    # Assert
    assert status == 202


def test_a_launch_past_the_deadline_answers_before_the_launch_finishes(
    slow_ok_spawn,
) -> None:
    """The point of the deadline: the ANSWER beats the WORK.

    Asserted as an ORDERING, not as a stopwatch reading. This used to be
    ``elapsed < _SHIM_SLEEP_S / 2`` — a 1.0 s wall-clock ceiling on a full HTTP
    round trip whose handler is only promised to *decide* within 0.3 s. The
    remaining 0.7 s was silently standing in for "and everything else the
    request touches is fast": TestClient's portal thread, the ACL check, the
    lineage read, JSON encode/decode. None of that is bounded, and CI
    runs the suite at ``-n 32`` on 32 cores, so the ceiling measured the BOX,
    not the deadline. The sibling failure in this same module on 2026-08-12
    (``marker is None``) was the same clock-for-event substitution.

    ``launch_still_running_at_answer`` reads the actual claim: at the moment the
    response came back, the shim had NOT yet reached its final act. That stays
    true however slow the host is, and it goes false for exactly the reason the
    deadline exists to prevent — the handler blocking until the launch is done.
    """
    # Arrange
    result = slow_ok_spawn
    # Act
    answered_first = result.launch_still_running_at_answer
    # Assert — without the deadline the handler blocks for the full shim sleep.
    assert answered_first is True


def test_a_202_body_reports_status_accepted(slow_ok_spawn) -> None:
    # Arrange
    result = slow_ok_spawn
    # Act
    status_field = result.body["status"]
    # Assert
    assert status_field == "accepted"


def test_a_202_body_reports_the_launch_phase(slow_ok_spawn) -> None:
    """``phase`` tells the caller whether the agent was even kicked off yet."""
    # Arrange
    result = slow_ok_spawn
    # Act
    phase = result.body["phase"]
    # Assert
    assert phase == "launch"


def test_a_202_body_names_a_poll_route(slow_ok_spawn) -> None:
    """202 is only honest if it says where the outcome will appear."""
    # Arrange
    result = slow_ok_spawn
    # Act
    poll = result.body["poll"]
    # Assert
    assert poll == "/agents/slow-ok/status"


def test_a_202_does_not_cancel_the_running_launch(slow_ok_spawn) -> None:
    """The shield: answering 202 must leave the spawn running, not kill it."""
    # Arrange
    result = slow_ok_spawn
    # Act
    completed = result.launch_completed
    # Assert
    assert completed is True


def test_a_failure_after_the_202_still_writes_its_marker(slow_failing_spawn) -> None:
    """A 202 must not be a degraded mode: a LATE failure stays diagnosable.

    Without the detach bookkeeping the task is abandoned, its rc and stderr go
    with it, and a caller polling ``/agents/<name>/status`` sees an agent that is
    merely "not running" with nothing saying why.
    """
    # Arrange
    result = slow_failing_spawn
    # Act
    marker = result.marker
    # Assert
    assert marker is not None


def test_a_late_failure_marker_carries_the_real_exit_code(slow_failing_spawn) -> None:
    # Arrange
    result = slow_failing_spawn
    # Act
    marker = result.marker or {}
    # Assert
    assert marker.get("exit_code") == 255


# ---------------------------------------------------------------------------
# The fast path is UNCHANGED — the deadline must not steal the definite answer
# ---------------------------------------------------------------------------


def test_a_launch_inside_the_deadline_still_answers_200(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """A synchronous 200 carries strictly more information than "poll me"."""
    # Arrange
    bin_dir = tmp_path / "sac_bin_fast"
    bin_dir.mkdir()
    _install_fast_sac_shim(bin_dir)
    _prepare_env(env_save_restore, bin_dir)
    # Act
    with TestClient(create_app(token=_TOKEN)) as client:
        resp = _post(client, "fast-child")
    # Assert
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The client/server contract that replaced two independently-picked constants
# ---------------------------------------------------------------------------


def test_a_client_timeout_outlives_the_server_deadline() -> None:
    """If this inverts, the client destroys the 202 it exists to receive.

    Not hypothetical: the pre-fix pair (server grace 20s, client budget 30s,
    both hand-picked in files that never import each other) left eight seconds
    of headroom on a host idling at load 60-70, and when it flipped the caller
    reported a SUCCEEDING spawn as failed.
    """
    # Arrange
    server = _handler_deadline.AGENT_START_DEADLINE_S
    # Act
    client = _handler_deadline.client_timeout_for(server)
    # Assert
    assert client > server


def test_a_client_timeout_tracks_a_moved_deadline(short_deadline) -> None:
    """The derivation must READ the deadline, not a copy taken at import.

    ``client_timeout_for`` used to spell its default
    ``server_deadline_s=AGENT_START_DEADLINE_S`` in the SIGNATURE, and a default
    argument is evaluated once, when the module is first imported — so it held
    the value and stopped tracking the name. The handler does not: it imports
    ``AGENT_START_DEADLINE_S`` inside the function body and honours a moved
    deadline on the next call. Move it and the server answers on the new budget
    while every client still waits on the old one; ``client > server`` inverts
    in silence, which is exactly the failure this module exists to prevent,
    arriving through the derivation that was supposed to prevent it.
    """
    # Arrange — the fixture has moved the server's declared deadline.
    moved = short_deadline
    # Act
    client = _handler_deadline.client_timeout_for()
    # Assert
    assert client == pytest.approx(moved + _handler_deadline.CLIENT_MARGIN_S)


def test_a_deadline_never_reports_negative_remaining() -> None:
    """Callers hand this straight to asyncio.wait_for, which rejects negatives."""
    # Arrange
    deadline = _handler_deadline.Deadline(0.0)
    # Act
    remaining = deadline.remaining()
    # Assert
    assert remaining == 0.0


def test_a_zero_budget_deadline_reports_expired() -> None:
    # Arrange
    deadline = _handler_deadline.Deadline(0.0)
    # Act
    expired = deadline.expired()
    # Assert
    assert expired is True
