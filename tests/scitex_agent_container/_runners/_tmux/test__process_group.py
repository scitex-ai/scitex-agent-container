from __future__ import annotations

import os
import signal
import subprocess
import sys

from scitex_agent_container._runners._tmux._process_group import (
    ProcessIdentity,
    capture_owned_process_group,
    capture_owned_process_tree,
    terminate_owned_process_group,
    terminate_owned_process_tree,
)


def test_term_escalates_to_kill_for_lingering_owned_child() -> None:
    # Arrange
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready = process.stdout.readline() if process.stdout is not None else ""
        snapshot = capture_owned_process_group(process.pid)
        # Act
        stopped = terminate_owned_process_group(
            snapshot, term_timeout_s=0.05, kill_timeout_s=1.0
        )
        process.wait(timeout=2)
        # Assert
        assert (ready, stopped, process.returncode) == (
            "ready\n",
            True,
            -signal.SIGKILL,
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_refuses_to_signal_callers_own_process_group() -> None:
    # Arrange
    pid = os.getpid()
    # Act
    snapshot = capture_owned_process_group(pid)
    # Assert
    assert snapshot == ()


def test_process_tree_refuses_success_without_a_dedicated_cgroup() -> None:
    # Arrange
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    parent_code = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = int(parent.stdout.readline()) if parent.stdout is not None else 0
    try:
        snapshot = capture_owned_process_tree(parent.pid)
        # Act
        stopped = terminate_owned_process_tree(snapshot)
        # Assert
        assert (
            stopped,
            {item.pid for item in snapshot},
        ) == (False, {parent.pid, child_pid})
    finally:
        for pid in (parent.pid, child_pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.wait(timeout=2)


def test_dedicated_cgroup_kill_catches_process_absent_from_snapshot() -> None:
    # Arrange
    identity = ProcessIdentity(
        pid=999_999_991,
        parent_pid=1,
        process_group=999_999_991,
        session=999_999_991,
        start_time=1,
        uid=os.getuid(),
        state="S",
        control_group="/sac-test.scope",
    )
    events: list[tuple[str, object]] = []
    empty_answers = iter((False, True))

    def record_group_signal(pgid: int, sig: int) -> None:
        events.append(("signal", (pgid, sig)))

    def record_cgroup_kill(control_group: str) -> bool:
        events.append(("cgroup.kill", control_group))
        return True

    # Act
    stopped = terminate_owned_process_tree(
        (identity,),
        killpg_fn=record_group_signal,
        cgroup_empty_fn=lambda _group: next(empty_answers),
        cgroup_kill_fn=record_cgroup_kill,
    )
    # Assert
    assert (stopped, events) == (
        True,
        [
            ("signal", (999_999_991, signal.SIGTERM)),
            ("cgroup.kill", "/sac-test.scope"),
        ],
    )
