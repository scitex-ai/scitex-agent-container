"""The timeout path must let a child clean up before it is destroyed.

INCIDENT (2026-07-18): three ``.git/index.lock`` files were stranded on the
shared ``~/proj/scitex-agent-container`` checkout; one of them left the
once-a-minute ``post-merge-pull`` sweep failing for 83 minutes with
``Unable to create '.../.git/index.lock': File exists``.

The producer mechanism, verified by strace rather than inferred: ``git status
--porcelain`` really does ``openat(".git/index.lock", O_RDWR|O_CREAT|O_EXCL)``,
and ``host_exec``'s timeout path sent the child's whole process GROUP a bare
SIGKILL. SIGKILL cannot be caught, so git's own lock cleanup never runs and the
file is stranded. A brokered call that ran a git op on a shared checkout and
exceeded its timeout stranded a lock every time.

The fix is a grace period, not the removal of the kill: SIGTERM the group, give
it a bounded moment to run its cleanup, then SIGKILL whatever is left. The
CONTROL tests below exist because "let the child clean up" is one careless edit
away from "stop killing the child" — which would restore the wedge that the
process-group kill was added to fix (see ``test__host_exec_wedge.py``).

No mocks (STX-NM002): real subprocesses, real signals, real files on disk. The
assertion is on the ARTIFACT — whether the lock file is gone — not on a return
code, because a return code cannot tell you whether cleanup ran.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import types
from pathlib import Path
from typing import Any

from scitex_agent_container._listen._host_exec import host_exec


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


def _dev_resolver(name: str) -> str:
    # Called by the handler as `group_resolver(name=caller)` — keyword, so the
    # parameter name is part of the seam's contract.
    _ = name
    return "developer"


def _noop_audit(_entry: dict[str, Any]) -> None:
    return None


def _exec(body: dict, *, node: str = "dev"):
    return host_exec(
        _FakeRequest(body, authenticated_node=node),
        group_resolver=_dev_resolver,
        audit_writer=_noop_audit,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# 1. THE FIX — a child gets to run its cleanup handler before it is destroyed.
# --------------------------------------------------------------------------


def test_timeout_lets_the_child_run_its_cleanup_handler_before_it_dies(tmp_path: Path):
    """RED against pre-fix: the lock file SURVIVED the timeout, because the
    group got a bare SIGKILL and the trap never ran. This is the incident's
    mechanism in miniature — the trap stands in for git's own atexit lock
    cleanup, which is likewise unreachable under SIGKILL.
    """
    # Arrange — a child that strands a lock file unless its TERM trap runs.
    lock = tmp_path / "index.lock"
    script = f'trap "rm -f {lock!s}; exit 143" TERM; touch {lock!s}; sleep 30'
    # Act — the call exceeds its deadline, so the timeout path destroys it.
    _run(_exec({"argv": ["bash", "-c", script], "timeout_s": 1.0}))
    # Assert — ask the artifact, not the exit code: the lock is NOT stranded.
    assert not lock.exists()


def test_the_cleanup_grace_does_not_stop_the_timeout_from_being_reported(
    tmp_path: Path,
):
    """A child that exits via its TERM trap still timed out. The grace period
    must not launder a timeout into an ordinary exit — the caller has to be
    able to tell a deadline miss from a clean run."""
    # Arrange
    lock = tmp_path / "index.lock"
    script = f'trap "rm -f {lock!s}; exit 143" TERM; touch {lock!s}; sleep 30'
    # Act
    payload = _resp_json(
        _run(_exec({"argv": ["bash", "-c", script], "timeout_s": 1.0}))
    )
    # Assert
    assert payload["timed_out"] is True


# --------------------------------------------------------------------------
# 2. CONTROLS — the kill must still happen. "Fixed it by not killing" fails
#    every test below.
# --------------------------------------------------------------------------


def test_a_child_that_ignores_sigterm_is_still_killed(tmp_path: Path):
    """CONTROL. The grace period is a courtesy, not a veto. A child that
    ignores SIGTERM (bash `trap "" TERM`, inherited as SIG_IGN by its
    children) must still be destroyed by the SIGKILL that follows.

    Without this, "let the child clean up" degrades into "the child decides
    whether to die", which is the unbounded-child wedge of INCIDENT 2026-07-17.
    """
    # Arrange — the whole group ignores SIGTERM; only SIGKILL can end it.
    script = 'trap "" TERM; sleep 30 & echo $!; sleep 30'
    # Act
    payload = _resp_json(
        _run(_exec({"argv": ["bash", "-c", script], "timeout_s": 1.0}))
    )
    grandchild_pid = int(payload["stdout"].strip())
    time.sleep(0.3)  # let the SIGKILL land
    # Assert — the SIGTERM-proof grandchild is gone anyway.
    assert not _pid_alive(grandchild_pid)


def test_a_child_that_ignores_sigterm_still_reports_the_process_group_kill(
    tmp_path: Path,
):
    """CONTROL. The typed outcome must still say the group was killed —
    a caller reading `killed_process_group` must not be told "no" merely
    because a SIGTERM was tried first."""
    # Arrange
    script = 'trap "" TERM; sleep 30'
    # Act
    payload = _resp_json(
        _run(_exec({"argv": ["bash", "-c", script], "timeout_s": 1.0}))
    )
    # Assert
    assert (payload["timed_out"], payload["killed_process_group"]) == (True, True)


def test_the_timeout_path_stays_bounded_even_for_a_sigterm_proof_child():
    """CONTROL. The grace must be BOUNDED. An unbounded wait-for-cleanup is
    just the original wedge wearing a politer name: the handler would never
    return and the whole host arm would be gone again."""
    # Arrange — ignores SIGTERM, so the full grace elapses before SIGKILL.
    script = 'trap "" TERM; sleep 30'
    # Act
    started = time.monotonic()
    _run(_exec({"argv": ["bash", "-c", script], "timeout_s": 1.0}))
    elapsed = time.monotonic() - started
    # Assert — deadline + grace + drain, with slack; NOT the child's 30s.
    assert elapsed < 15.0


def test_a_prompt_child_is_not_delayed_by_the_grace_period():
    """CONTROL. The grace belongs to the TIMEOUT path only. A command that
    finishes normally must not pay for it — a fix that charges every call a
    few seconds would be a performance regression across the whole fleet."""
    # Arrange
    body = {"argv": ["echo", "prompt"], "timeout_s": 10.0}
    started = time.monotonic()
    # Act
    payload = _resp_json(_run(_exec(body)))
    elapsed = time.monotonic() - started
    # Assert
    assert (payload["stdout"].strip(), elapsed < 2.0) == ("prompt", True)
