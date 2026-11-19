"""Watchers beside a sealed container: deadline and stdout/stderr tee (real processes)."""

from __future__ import annotations

import io
import subprocess
import sys

from scitex_agent_container._sandbox._watch import Deadline, start_tee

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_deadline_terminates_a_long_running_child():
    # Arrange
    child = subprocess.Popen(SLEEPER)
    deadline = Deadline(0.2, child, grace_s=5)
    # Act
    deadline.start()
    rc = child.wait(timeout=20)
    # Assert
    assert (rc, deadline.fired) == (-15, True)


def test_stopped_deadline_never_fires():
    # Arrange
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = Deadline(30, child)
    deadline.start()
    child.wait(timeout=20)
    # Act
    deadline.stop_evt.set()
    deadline.join(timeout=5)
    # Assert
    assert deadline.fired is False


def test_tee_forwards_output_to_the_caller(tmp_path):
    # Arrange
    child = subprocess.Popen(
        [sys.executable, "-c", "print('hello sealed')"], stdout=subprocess.PIPE
    )
    caller = io.BytesIO()
    # Act
    start_tee(child.stdout, caller, tmp_path / "stdout.log").join(timeout=20)
    child.wait(timeout=20)
    # Assert
    assert caller.getvalue() == b"hello sealed\n"


def test_tee_keeps_a_durable_copy(tmp_path):
    # Arrange
    child = subprocess.Popen(
        [sys.executable, "-c", "print('hello sealed')"], stdout=subprocess.PIPE
    )
    # Act
    start_tee(child.stdout, io.BytesIO(), tmp_path / "stdout.log").join(timeout=20)
    child.wait(timeout=20)
    # Assert
    assert (tmp_path / "stdout.log").read_bytes() == b"hello sealed\n"
