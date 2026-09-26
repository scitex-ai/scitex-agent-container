from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace

from scitex_agent_container._runners import _scope_ownership
from scitex_agent_container._runners._scope_ownership import (
    capture_scope_ownership,
    ensure_owned_scope_down,
)
from scitex_agent_container._runners._tmux._process_group import ProcessIdentity

_CGROUP = "/user.slice/user-1000.slice/user@1000.service/tmux-spawn-a.scope"
_UNIT = "tmux-spawn-a.scope"
_INVOCATION = "a" * 32


def _identity(pid: int, *, start: int = 17) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        parent_pid=1,
        process_group=pid,
        session=pid,
        start_time=start,
        uid=os.getuid(),
        state="S",
        control_group=_CGROUP,
    )


def _record() -> dict:
    return {
        "pid": 41,
        "process_start_time": 17,
        "process_uid": os.getuid(),
        "control_group": _CGROUP,
        "scope_unit": _UNIT,
        "scope_invocation_id": _INVOCATION,
    }


def test_capture_binds_pid_birth_and_scope_invocation() -> None:
    # Arrange
    shown = {
        "ControlGroup": _CGROUP,
        "InvocationID": _INVOCATION,
        "ActiveState": "active",
    }
    # Act
    ownership = capture_scope_ownership(
        41, identity_fn=lambda _pid: _identity(41), show_scope_fn=lambda _unit: shown
    )
    # Assert
    assert ownership is not None and ownership.to_record_fields() == _record()


def test_mismatched_invocation_is_never_stopped() -> None:
    # Arrange
    stop_calls: list[str] = []
    shown = {"ControlGroup": _CGROUP, "InvocationID": "b" * 32}
    # Act
    stopped = ensure_owned_scope_down(
        _record(),
        identity_fn=lambda _pid: _identity(41),
        show_scope_fn=lambda _unit: shown,
        cgroup_pids_fn=lambda _group: (41,),
        stop_scope_fn=lambda unit: not stop_calls.append(unit),
    )
    # Assert
    assert (stopped, stop_calls) == (False, [])


def test_scope_stop_waits_for_process_tree_disappearance() -> None:
    # Arrange
    pid_answers: Iterator[tuple[int, ...]] = iter(((41, 42), (41,), ()))
    live = {41: True, 42: True}
    shown = {"ControlGroup": _CGROUP, "InvocationID": _INVOCATION}

    def identity(pid: int) -> ProcessIdentity | None:
        return _identity(pid) if live.get(pid, False) else None

    def pids(_group: str) -> tuple[int, ...]:
        answer = next(pid_answers)
        if not answer:
            live.clear()
        return answer

    # Act
    stopped = ensure_owned_scope_down(
        _record(),
        identity_fn=identity,
        show_scope_fn=lambda _unit: shown,
        cgroup_pids_fn=pids,
        stop_scope_fn=lambda _unit: True,
        sleep_fn=lambda _seconds: None,
    )
    # Assert
    assert stopped is True


def test_nonempty_scope_cannot_report_stopped() -> None:
    # Arrange
    shown = {"ControlGroup": _CGROUP, "InvocationID": _INVOCATION}
    # Act
    stopped = ensure_owned_scope_down(
        _record(),
        identity_fn=lambda pid: _identity(pid),
        show_scope_fn=lambda _unit: shown,
        cgroup_pids_fn=lambda _group: (41,),
        stop_scope_fn=lambda _unit: True,
        sleep_fn=lambda _seconds: None,
        timeout_s=0,
    )
    # Assert
    assert stopped is False


def test_scope_stop_request_does_not_block_behind_systemd() -> None:
    # Arrange
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    # Act
    stopped = _scope_ownership._stop_scope(_UNIT, run_fn=run)
    # Assert
    assert (stopped, calls) == (
        True,
        [
            (
                ["systemctl", "--user", "stop", "--no-block", _UNIT],
                {"capture_output": True, "text": True, "timeout": 5},
            )
        ],
    )


def test_default_wait_covers_systemd_scope_drain() -> None:
    # Arrange — the real Hermes canary needed about 35 seconds for its scope
    # to transition from stop-sigterm to dead.  systemd's default stop window
    # is 90 seconds, so five seconds is not a terminal observation.
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def identity(pid: int) -> ProcessIdentity | None:
        return _identity(pid) if clock[0] < 35.0 else None

    def pids(_group: str) -> tuple[int, ...]:
        return (41,) if clock[0] < 35.0 else ()

    shown = {"ControlGroup": _CGROUP, "InvocationID": _INVOCATION}
    # Act
    stopped = ensure_owned_scope_down(
        _record(),
        identity_fn=identity,
        show_scope_fn=lambda _unit: shown,
        cgroup_pids_fn=pids,
        stop_scope_fn=lambda _unit: True,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock[0],
    )
    # Assert
    assert (stopped, clock[0] >= 35.0) == (True, True)
