"""Failure snapshots read only owned native-child metadata, never arguments."""

import subprocess
import sys

import pytest

from ._a2a_proxy_main_fixture import _snapshot


@pytest.fixture
def owned_native_snapshot(tmp_path):
    """A disposable child; no proxy, network, credentials or operator files."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child, _snapshot(child, tmp_path)
    finally:
        child.kill()
        child.wait(timeout=2)


def test_owned_snapshot_has_real_nonnegative_cpu_and_io_counters(owned_native_snapshot):
    # Arrange
    child, snapshot = owned_native_snapshot
    # Act
    cpu = snapshot["cpu_seconds"]
    io = snapshot["io_bytes"]
    # Assert
    assert (
        snapshot["pid"] == child.pid,
        cpu["user"] >= 0,
        cpu["system"] >= 0,
        io["read"] >= 0,
        io["write"] >= 0,
    ) == (True, True, True, True, True)


def test_owned_snapshot_has_age_and_scheduler_observations(owned_native_snapshot):
    # Arrange
    _child, snapshot = owned_native_snapshot
    # Act
    switches = snapshot["context_switches"]
    # Assert
    assert (
        snapshot["process_age_seconds"] >= 0,
        switches["voluntary"] >= 0,
        switches["involuntary"] >= 0,
    ) == (True, True, True)


def test_exited_child_snapshot_stops_before_proc_metadata(tmp_path):
    # Arrange
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    # Act
    snapshot = _snapshot(child, tmp_path)
    # Assert
    assert snapshot == {"pid": child.pid, "returncode": 0, "kernel_state": "exited"}
