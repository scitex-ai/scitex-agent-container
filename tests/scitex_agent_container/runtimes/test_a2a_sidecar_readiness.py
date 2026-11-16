"""A PID's existence is not native sidecar HTTP readiness (real owned resources)."""

import socket
import subprocess
import sys
import time

import psutil
import pytest

from tests.scitex_agent_container.runtimes._a2a_sidecar_fixture import (
    shared_workdir_pair as shared_workdir_pair,
)
from tests.scitex_agent_container.runtimes._a2a_sidecar_fixture import (
    wait_for_sidecar,
)


def test_exited_unreaped_child_cannot_qualify_as_ready():
    # Arrange — kill(pid,0) succeeds and /proc argv is empty for this zombie.
    process = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 2
        while psutil.Process(process.pid).status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() > deadline:
                raise TimeoutError("Owned child did not exit")
            time.sleep(0.01)

        # Act
        def wait():
            return wait_for_sidecar(process.pid, 1, "fixture", timeout=0.2)

        # Assert
        with pytest.raises(RuntimeError, match="exited"):
            wait()
    finally:
        process.wait(timeout=2)


def test_live_child_without_an_http_listener_is_not_ready():
    # Arrange — the real child's argv includes the target port, but it serves nothing.
    with socket.socket() as claimed:
        claimed.bind(("127.0.0.1", 0))
        port = claimed.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(port)]
    )
    try:
        # Act
        def wait():
            return wait_for_sidecar(process.pid, port, "fixture", timeout=0.2)

        # Assert
        with pytest.raises(TimeoutError):
            wait()
    finally:
        process.terminate()
        process.wait(timeout=2)


def test_foreign_listener_does_not_prove_child_socket_ownership():
    # Arrange — the parent owns the port; the unrelated child only names it.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", str(port)]
        )
        try:
            # Act
            def wait():
                return wait_for_sidecar(process.pid, port, "fixture", timeout=0.2)

            # Assert
            with pytest.raises(TimeoutError):
                wait()
        finally:
            process.terminate()
            process.wait(timeout=2)


def test_actual_agent_metadata_must_match_the_owned_child(shared_workdir_pair):
    # Arrange — bravo is a genuine native CLI server; alpha is the wrong identity.
    pair = shared_workdir_pair

    # Act
    def wait():
        return wait_for_sidecar(
            pair["pid_b"], pair["port_b"], "pair-alpha", timeout=0.2
        )

    # Assert
    with pytest.raises(TimeoutError):
        wait()


def _failure_from_exited_child(startup_log):
    process = subprocess.Popen(["true"])
    process.wait(timeout=2)
    try:
        return wait_for_sidecar(
            process.pid, 1, "owned-fixture", timeout=0.2, log_path=startup_log
        )
    except RuntimeError as failure:
        return failure


def test_exited_child_reports_only_bounded_owned_startup_log(tmp_path):
    # Arrange: a real reaped child and synthetic diagnostic data, not a live sidecar.
    startup_log = tmp_path / "startup.log"
    startup_log.write_bytes(
        b"discarded-prefix" + b"x" * 5000 + b"owned diagnostic tail"
    )
    # Act
    failure = _failure_from_exited_child(startup_log)
    # Assert
    assert (
        isinstance(failure, RuntimeError)
        and "owned diagnostic tail" in str(failure)
        and "discarded-prefix" not in str(failure)
        and "pid=" in str(failure)
        and "owned-fixture" in str(failure)
        and "exited before HTTP readiness" in str(failure)
    )


def test_missing_owned_startup_log_does_not_mask_child_exit(tmp_path):
    # Arrange
    startup_log = tmp_path / "absent.log"
    # Act
    failure = _failure_from_exited_child(startup_log)
    # Assert
    assert (
        isinstance(failure, RuntimeError)
        and "exited before HTTP readiness" in str(failure)
        and "unavailable" in str(failure)
    )
