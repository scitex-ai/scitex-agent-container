"""Declining to double-run is a SKIP, not a failure.

MEASURED (2026-09-07, scitex-compute-03). The single-flight lock added in
#1320/#1322 works: a second ``sac image bake-remote`` correctly refused while
pid 854661 held the lock and rsynced from spartan. But the refusal went through
``fail``, so it emitted ``{"verdict":"FAILED"}`` and exited 1, and
``scitex-agent-container-spartan-sif-bake.service`` sat in ``failed``:

    FATAL[workdir]: already-running another bake of layer=base holds the lock

That is a REGRESSION IN THE REPORTING, not in the locking. The unit's own
timeout is 14400s and a bake runs for hours, so the timer overlapping a running
bake is NORMAL — under the old behaviour every normal overlap painted the unit
red, which is how a real failure would have been lost among expected ones. The
fleet already had eleven red units nobody could triage for exactly this reason:
findings and refusals were indistinguishable from breakage.

``SKIPPED`` needs no new vocabulary and no change on the reading side — it is
already an ok verdict to the caller (``_remote_bake_core``: ``if self.verdict in
(BakeVerdict.BAKED, BakeVerdict.SKIPPED)``), and the script already emits it for
``source-unchanged``.

Deliberately NOT fixed with ``SuccessExitStatus=`` on the unit: that neutralises
the exit code for EVERY reason, so it would swallow the real failures too. This
fleet has already been bitten by that exact pair — a stale-lock monitor exiting
1 on a genuine detection into a unit declaring ``SuccessExitStatus=0 1``, where
neither half was wrong on its own and the detection silently rendered as
success.

The tests below run the shipped script's own lock fragment, lifted verbatim, so
they break if it is restructured rather than passing against a file that no
longer contains the mechanism. The CONTROLS matter as much as the assertion: a
"skip" is trivially achievable by never failing at all, so the failure path must
still be shown to fail, and the lock must still be shown to admit a caller when
it is free.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

import scitex_agent_container

BAKE_SCRIPT = (
    Path(scitex_agent_container.__file__).resolve().parent
    / "containers"
    / "spartan-sif-bake.sh"
)

LOCK_LINE = (
    'flock -n 9 || skip "already-running" '
    '"another bake of layer=$LAYER holds the lock"'
)


def _lift_lock_line() -> str:
    """The shipped script's OWN flock line, lifted verbatim.

    The behavioural tests below must run the real line, not ``LOCK_LINE``. A
    first draft of this file built its harness from that constant, and a mutant
    that routed the refusal back through ``fail()`` left 8 of 9 tests GREEN --
    they were exercising the test's own copy, so they would have kept passing
    forever after the script stopped skipping. Lifting it means the mutant turns
    the behaviour red, which is the only reason to write these tests at all.
    """
    for line in BAKE_SCRIPT.read_text().splitlines():
        if line.startswith("flock -n 9 ||"):
            return line
    raise AssertionError("the shipped script no longer has a 'flock -n 9 ||' line")


def _lift(marker: str) -> str:
    """Return the shipped script's block starting at ``marker``.

    Lifted verbatim rather than retyped: a test that carries its own copy of
    the logic passes forever after the real script stops doing it.
    """
    text = BAKE_SCRIPT.read_text()
    assert marker in text, f"the shipped script no longer contains: {marker!r}"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _run_lock_fragment(workdir: Path, *, layer: str = "base") -> subprocess.CompletedProcess:
    """Run the script's real lock line with its real skip()/fail() helpers."""
    (workdir / "state").mkdir(parents=True, exist_ok=True)
    program = "\n".join(
        [
            "set -u",
            f'LAYER="{layer}"',
            'STEP="workdir"',
            f'WORKDIR="{workdir}"',
            _lift("fail() {"),
            _lift("skip() {"),
            'exec 9>"$WORKDIR/state/bake-$LAYER.lock"',
            _lift_lock_line(),
            'echo "REACHED_PAST_LOCK"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, timeout=60
    )


@pytest.fixture()
def held_lock(tmp_path: Path):
    """Hold the layer lock from another process, as a running bake would."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    fd = os.open(str(tmp_path / "state" / "bake-base.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield tmp_path
    finally:
        os.close(fd)


def test_the_shipped_script_routes_the_lock_refusal_through_skip():
    # Arrange: the shipped script, not a copy of its logic
    script = BAKE_SCRIPT

    # Act
    text = script.read_text()

    # Assert
    assert LOCK_LINE in text, (
        "the single-flight refusal must call skip(), not fail(): a systemd "
        "timer overlapping a multi-hour bake is normal, and rendering it as a "
        "unit failure buries the real failures"
    )


def test_a_refused_bake_exits_ZERO(held_lock):
    # Arrange: another process already holds the layer lock (fixture)
    workdir = held_lock

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert result.returncode == 0, result.stderr


def test_a_refused_bake_emits_the_SKIPPED_verdict(held_lock):
    # Arrange: another process already holds the layer lock (fixture)
    workdir = held_lock

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert '"verdict":"SKIPPED"' in result.stdout, result.stdout


def test_a_refused_bake_never_emits_FAILED(held_lock):
    # Arrange: another process already holds the layer lock (fixture)
    workdir = held_lock

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert '"verdict":"FAILED"' not in result.stdout, result.stdout


def test_the_refusal_names_the_reason_so_the_verdict_is_not_anonymous(held_lock):
    # Arrange: another process already holds the layer lock (fixture)
    workdir = held_lock

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert '"reason":"already-running"' in result.stdout, result.stdout


def test_a_refused_bake_does_NOT_continue_past_the_lock(held_lock):
    # Arrange: another process already holds the layer lock (fixture)
    workdir = held_lock

    # Act
    result = _run_lock_fragment(workdir)

    # Assert — exiting 0 must not mean "carry on and bake anyway"
    assert "REACHED_PAST_LOCK" not in result.stdout, result.stdout


# --- CONTROLS -------------------------------------------------------------
# A skip is trivially achievable by never failing and by never locking. Both
# halves are pinned so this suite cannot pass against a script that dropped the
# mechanism it claims to test.


def test_CONTROL_an_UNHELD_lock_admits_the_caller(tmp_path):
    # Arrange: nobody holds the lock
    workdir = tmp_path

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert "REACHED_PAST_LOCK" in result.stdout, result.stdout


def test_CONTROL_the_unheld_run_emits_no_SKIPPED_verdict(tmp_path):
    # Arrange: nobody holds the lock
    workdir = tmp_path

    # Act
    result = _run_lock_fragment(workdir)

    # Assert
    assert "SKIPPED" not in result.stdout, result.stdout


def test_CONTROL_the_failure_path_still_FAILS_and_exits_nonzero(tmp_path):
    # Arrange — fail() must be untouched by this change
    program = "\n".join(
        [
            "set -u",
            'LAYER="base"',
            'STEP="workdir"',
            _lift("fail() {"),
            'fail "workdir-create" "/nonexistent"',
        ]
    )

    # Act
    result = subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, timeout=60
    )

    # Assert
    assert result.returncode != 0 and '"verdict":"FAILED"' in result.stdout, (
        result.returncode,
        result.stdout,
    )
