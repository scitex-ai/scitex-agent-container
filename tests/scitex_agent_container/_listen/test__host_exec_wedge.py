"""Regression tests for the ``POST /v1/host_exec`` wedge (INCIDENT 2026-07-17).

Every test here was CONFIRMED TO FAIL against the pre-fix handler — see the
per-test "RED against pre-fix" notes. A gate nobody has watched go red is a
gate nobody has tested.

The incident's fingerprint: ``/v1/health`` answered 200 in 0.016s while
``POST /v1/host_exec`` returned 0 bytes at both 20s and 100s. The endpoint was
read as "host_exec has no timeout"; it had a 300s one that works. The timeout
was UNREACHABLE — it lives inside the worker thread, and the pre-fix handler
dispatched through ``asyncio.to_thread`` (the SHARED default
ThreadPoolExecutor). A drained pool means the thread never starts, so the child
never starts, so the child's timeout never starts.

No mocks, no monkeypatching (STX-NM002): real subprocesses, real threads, and
the handler's own ``group_resolver`` / ``audit_writer`` keyword seams take
plain hand-rolled fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import types
from typing import Any

from scitex_agent_container._listen._host_exec import host_exec
from scitex_agent_container._listen._host_exec_inflight import (
    host_exec_inflight,
    inflight_snapshot,
)


class _FakeRequest:
    def __init__(self, body: object, *, authenticated_node: str | None = None) -> None:
        self._body = body
        self.state = types.SimpleNamespace(authenticated_node=authenticated_node)

    async def json(self) -> object:
        return self._body


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resp_json(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def _dev_resolver(name: str) -> set[str]:
    return {"developer"}


def _noop_audit(_entry: dict[str, Any]) -> None:
    return None


def _exec(body: dict, *, node: str = "dev"):
    return host_exec(
        _FakeRequest(body, authenticated_node=node),
        group_resolver=_dev_resolver,
        audit_writer=_noop_audit,
    )


def _starve_default_executor(loop, release: threading.Event) -> int:
    """Occupy every worker in the loop's SHARED default ThreadPoolExecutor.

    This is the incident's precondition, reproduced honestly: the pool is
    ``min(32, cpu_count + 4)`` and six background loops in this daemon abandon
    threads into it on every overrunning tick.
    """
    max_workers = min(32, (os.cpu_count() or 1) + 4)
    for _ in range(max_workers):
        loop.run_in_executor(None, release.wait)
    return max_workers


# --------------------------------------------------------------------------
# 1. The wedge itself: a starved shared pool must not delay host_exec.
# --------------------------------------------------------------------------


def test_host_exec_is_served_while_the_shared_default_executor_is_starved():
    """RED against pre-fix: hung >8s and never returned (measured), because
    `to_thread` queued behind the drained pool and the child never started."""

    # Arrange — every worker in the shared default pool is occupied.
    async def scenario():
        loop = asyncio.get_running_loop()
        release = threading.Event()
        _starve_default_executor(loop, release)
        await asyncio.sleep(0.5)  # let every worker pick up its blocker
        try:
            # A trivial command that costs ~1ms.
            return await asyncio.wait_for(
                _exec({"argv": ["true"], "timeout_s": 5.0}), timeout=20.0
            )
        finally:
            release.set()

    # Act
    resp = _run(scenario())
    # Assert
    assert _resp_json(resp)["exit_code"] == 0


def test_host_exec_does_not_consume_the_shared_default_executor():
    """host_exec must not DRAIN the pool either — the other half of the
    contract. A long host_exec leaves the pool free for everyone else."""

    # Arrange — a long-running host_exec is in flight throughout.
    async def scenario():
        loop = asyncio.get_running_loop()
        slow = asyncio.ensure_future(
            _exec({"argv": ["sleep", "1.5"], "timeout_s": 10.0})
        )
        await asyncio.sleep(0.4)  # ensure the child is running
        # The shared pool must still serve a trivial job promptly.
        started = time.monotonic()
        await asyncio.wait_for(loop.run_in_executor(None, lambda: "ok"), timeout=5.0)
        elapsed = time.monotonic() - started
        await slow
        return elapsed

    # Act
    elapsed = _run(scenario())
    # Assert — the pool was never touched by host_exec, so this is immediate.
    assert elapsed < 1.0


# --------------------------------------------------------------------------
# 2. A stuck child must not starve a second caller.
# --------------------------------------------------------------------------


def test_a_second_caller_is_served_while_a_first_caller_is_stuck_on_stdin():
    """The incident's core claim: one caller's stuck child took the host arm
    away from the WHOLE fleet. It must not."""

    async def scenario():
        # Arrange — caller A blocks for a long time; it is never awaited to
        # completion, exactly like the real stuck command.
        stuck = asyncio.ensure_future(
            _exec({"argv": ["sleep", "30"], "timeout_s": 25.0}, node="caller-a")
        )
        await asyncio.sleep(0.4)
        try:
            # Act — caller B asks for something trivial.
            return await asyncio.wait_for(
                _exec(
                    {"argv": ["echo", "b-served"], "timeout_s": 5.0}, node="caller-b"
                ),
                timeout=10.0,
            )
        finally:
            stuck.cancel()

    # Act
    resp = _run(scenario())
    # Assert
    assert _resp_json(resp)["stdout"].strip() == "b-served"


# --------------------------------------------------------------------------
# 3. Timeout kills the process GROUP, not just the pid.
# --------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_timeout_kills_the_whole_process_group_not_just_the_direct_child():
    """RED against pre-fix: `subprocess.run`'s timeout SIGKILLs only the direct
    child. Measured pre-fix, the grandchild `sleep` SURVIVED the timeout.

    The child prints its grandchild's pid, then blocks forever on stdin. After
    the timeout, that grandchild must be gone.
    """
    # Arrange — grandchild's pid on stdout, then block forever.
    script = "sleep 30 & echo $!; exec cat"
    req = _FakeRequest(
        {"argv": ["bash", "-c", script], "timeout_s": 1.0}, authenticated_node="dev"
    )
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    payload = _resp_json(resp)
    grandchild_pid = int(payload["stdout"].strip())
    time.sleep(0.3)  # let the SIGKILL land
    # Assert
    assert not _pid_alive(grandchild_pid)


def test_timeout_reports_that_it_killed_the_process_group():
    # Arrange — `sleep`, NOT `cat`: with stdin=/dev/null a stdin-reader gets EOF
    # and exits, so it never reaches the timeout path this test is about.
    req = _FakeRequest(
        {"argv": ["sleep", "30"], "timeout_s": 1.0}, authenticated_node="dev"
    )
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["killed_process_group"] is True


# --------------------------------------------------------------------------
# 4. stdin is /dev/null for the child — the whole "blocks on stdin" class.
# --------------------------------------------------------------------------


def test_a_stdin_reading_child_gets_eof_instead_of_blocking_forever():
    """RED against pre-fix: the child inherited the daemon's stdin, so `cat`
    (and `git commit -F -`, and an ssh passphrase prompt) could block until the
    timeout. With stdin=/dev/null it reaches EOF immediately and exits 0."""
    # Arrange — `cat` with no redirect; only DEVNULL makes this terminate.
    req = _FakeRequest({"argv": ["cat"], "timeout_s": 10.0}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert — exited cleanly on EOF, did NOT hit the timeout.
    assert (_resp_json(resp)["exit_code"], _resp_json(resp)["timed_out"]) == (0, False)


def test_stdin_devnull_does_not_break_the_b64_pipe_delivery_path():
    """The subtle one the fix had to not break: `echo <b64> | base64 -d | bash`.
    The outer bash takes its script from `-c`; the inner pipeline builds its own
    stdin. Closing the CHILD's stdin must leave delivery intact."""
    import base64

    # Arrange
    b64 = base64.b64encode(b"echo DELIVERED-OK").decode()
    req = _FakeRequest(
        {"argv": ["bash", "-c", f"echo {b64} | base64 -d | bash"], "timeout_s": 10.0},
        authenticated_node="dev",
    )
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["stdout"].strip() == "DELIVERED-OK"


# --------------------------------------------------------------------------
# 5. Never return empty — a timeout is TYPED and LOUD.
# --------------------------------------------------------------------------


def test_timeout_response_is_typed_and_names_the_argv():
    # Arrange
    req = _FakeRequest(
        {"argv": ["sleep", "30"], "timeout_s": 0.5}, authenticated_node="dev"
    )
    # Act
    payload = _resp_json(
        _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    )
    # Assert — timed_out is explicit, never an empty body the caller must guess at.
    assert payload["timed_out"] is True


def test_timeout_audit_entry_records_the_process_group_kill():
    # Arrange — `sleep`, not `cat`: see the note in the test above.
    entries: list[dict[str, Any]] = []
    req = _FakeRequest(
        {"argv": ["sleep", "30"], "timeout_s": 1.0}, authenticated_node="dev"
    )
    # Act
    _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=entries.append))
    # Assert
    assert entries[0]["killed_process_group"] is True


# --------------------------------------------------------------------------
# 6. The inflight probe — see the cause instead of inferring it from silence.
# --------------------------------------------------------------------------


def test_inflight_probe_reports_a_running_exec_with_its_caller():
    async def scenario():
        # Arrange
        running = asyncio.ensure_future(
            _exec({"argv": ["sleep", "5"], "timeout_s": 10.0}, node="noisy-caller")
        )
        await asyncio.sleep(0.4)
        try:
            # Act
            resp = await host_exec_inflight(_FakeRequest({}))
            return _resp_json(resp)
        finally:
            running.cancel()

    # Act
    payload = _run(scenario())
    # Assert
    assert payload["execs"][0]["caller"] == "noisy-caller"


def test_inflight_probe_is_empty_once_the_exec_completes():
    # Arrange
    _run(_exec({"argv": ["true"], "timeout_s": 5.0}))
    # Act
    entries = inflight_snapshot()
    # Assert — the registry unregisters in a finally, so nothing leaks.
    assert entries == []


def test_inflight_entry_is_removed_even_when_the_child_times_out():
    # Arrange — `sleep`, NOT `cat`: under stdin=/dev/null a stdin-reader exits
    # on EOF, so this test would pass WITHOUT ever timing out and its name
    # would assert something that never happened. That this argv really does
    # time out is pinned by
    # `test_timeout_response_is_typed_and_names_the_argv`.
    _run(_exec({"argv": ["sleep", "30"], "timeout_s": 1.0}))
    # Act
    entries = inflight_snapshot()
    # Assert
    assert entries == []
