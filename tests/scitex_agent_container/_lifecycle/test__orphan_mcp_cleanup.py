"""Tests for ``_lifecycle/_orphan_mcp_cleanup.kill_orphan_mcp_children``.

STX-TQ002 AAA + STX-TQ007 one-assert. NO mocks (no ``unittest.mock``, no
``monkeypatch`` parameter) — the production helper exposes three real
seams (``process_iter``, ``kill_fn``, ``self_pid``) explicitly for this
purpose. Each test passes a hand-rolled :class:`_FakeProcess` list and a
:class:`_KillRecorder` callable. The fakes match the ``proc.info``-dict
shape that ``psutil.process_iter(["pid", "cmdline", "environ"])`` yields,
so the cleanup walks them through the exact code path it runs in prod.

Coverage:
  * dry_run returns the candidate PID list without invoking the killer.
  * empty fleet → empty list.
  * psutil missing / OSError on iter → defensive return ``[]``, no raise.
  * self-PID is excluded (paranoia guard — never SIGKILL ourselves).
  * env-mismatch skip (different agent's orphan is NOT killed).
  * cmdline-mismatch skip (env matches but cmdline isn't MCP).
  * real-kill path invokes the killer with SIGKILL on the candidate PID.
"""

from __future__ import annotations

import signal

from scitex_agent_container._lifecycle._orphan_mcp_cleanup import (
    kill_orphan_mcp_children,
)

# ---------------------------------------------------------------------------
# Real test seams (no mocks; explicit hand-rolled fakes)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Stand-in for a ``psutil.Process`` carrying the ``info`` dict shape.

    ``psutil.process_iter(["pid", "cmdline", "environ"])`` populates each
    yielded process's ``info`` attribute with exactly those three keys.
    The cleanup helper reads through ``proc.info`` first and only falls
    back to method calls when ``info`` is absent — matching that shape
    here exercises the real prod code path.
    """

    def __init__(self, pid: int, cmdline: list[str], environ: dict[str, str]) -> None:
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline, "environ": environ}


class _KillRecorder:
    """Records ``(pid, sig)`` calls in lieu of touching ``os.kill``."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))


def _agent_env(name: str) -> dict[str, str]:
    """Real env dict shape — agent name keyed under the canonical env-var."""
    return {"SCITEX_AGENT_CONTAINER_NAME": name, "PATH": "/usr/bin"}


def _orphan_telegrammer(pid: int, name: str) -> _FakeProcess:
    """A canonical telegrammer-orphan fake matching both signals."""
    return _FakeProcess(
        pid=pid,
        cmdline=["bun", "run", "claude-code-telegrammer/server.ts"],
        environ=_agent_env(name),
    )


# ---------------------------------------------------------------------------
# dry_run path — returns candidates, kills nothing
# ---------------------------------------------------------------------------


def test_dry_run_returns_orphan_pid_without_killing() -> None:
    # Arrange
    orphan = _orphan_telegrammer(pid=4242, name="alpha")  # stx-allow: STX-NL001
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "alpha",
        dry_run=True,
        process_iter=lambda: [orphan],
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([4242], [])  # stx-allow: STX-NL001


def test_dry_run_with_multiple_orphans_returns_all_candidate_pids() -> None:
    # Arrange
    procs = [
        _orphan_telegrammer(pid=100, name="beta"),  # stx-allow: STX-NL001
        _orphan_telegrammer(pid=101, name="beta"),  # stx-allow: STX-NL001
    ]
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "beta",
        dry_run=True,
        process_iter=lambda: procs,
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert sorted(killed) == [100, 101]  # stx-allow: STX-NL001


# ---------------------------------------------------------------------------
# Empty fleet — no orphans, empty list
# ---------------------------------------------------------------------------


def test_empty_process_snapshot_returns_empty_list() -> None:
    # Arrange — no processes at all (a freshly booted host or a host with
    # zero agents in their previous-incarnation MCP children).
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "gamma",
        process_iter=list,
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


# ---------------------------------------------------------------------------
# Defensive fallback paths — NEVER raises
# ---------------------------------------------------------------------------


def test_process_iter_raising_oserror_returns_empty_list() -> None:
    # Arrange — psutil iter raises OSError on restricted /proc fs or under
    # cgroup limits; cleanup must degrade to "no orphans found" and the
    # caller (agent_start) must proceed.
    def _raise() -> list:
        raise OSError("permission denied on /proc")

    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "delta",
        process_iter=_raise,
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


def test_process_iter_raising_modulenotfounderror_returns_empty_list() -> None:
    # Arrange — psutil import failure is reified by the helper as
    # ModuleNotFoundError from the lazy ``import psutil`` inside the
    # default iterator. Tests substitute the iterator directly and raise
    # the same class to assert the defensive contract.
    def _raise() -> list:
        raise ModuleNotFoundError("No module named 'psutil'")

    # Act
    killed = kill_orphan_mcp_children(
        "epsilon",
        process_iter=_raise,
        kill_fn=_KillRecorder(),
        self_pid=1,
    )
    # Assert
    assert killed == []


def test_arbitrary_exception_during_iter_returns_empty_list() -> None:
    # Arrange — catch-all guarantee: ANY exception from process_iter must
    # NOT propagate. A random RuntimeError would otherwise wedge restart.
    def _raise() -> list:
        raise RuntimeError("unexpected psutil failure on flaky host")

    # Act
    killed = kill_orphan_mcp_children(
        "zeta",
        process_iter=_raise,
        kill_fn=_KillRecorder(),
        self_pid=1,
    )
    # Assert
    assert killed == []


# ---------------------------------------------------------------------------
# Match policy — self-PID, env, and cmdline filters
# ---------------------------------------------------------------------------


def test_current_process_excluded_from_kill_list() -> None:
    # Arrange — the sac CLI's own pid may share the env (it set
    # SCITEX_AGENT_CONTAINER_NAME for hook propagation) — must never
    # SIGKILL ourselves. ``self_pid`` parameter wires the guard.
    orphan = _orphan_telegrammer(pid=999, name="eta")  # stx-allow: STX-NL001
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "eta",
        process_iter=lambda: [orphan],
        kill_fn=killer,
        self_pid=999,  # stx-allow: STX-NL001 — same PID → excluded
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


def test_process_with_different_agent_env_is_skipped() -> None:
    # Arrange — env carries a *different* agent name; cleanup must NOT
    # cross agent boundaries (an orphan of agent 'theta' is theta's
    # problem, never iota's).
    other_proc = _orphan_telegrammer(pid=222, name="theta")  # stx-allow: STX-NL001
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "iota",
        process_iter=lambda: [other_proc],
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


def test_process_without_mcp_cmdline_is_skipped() -> None:
    # Arrange — env identifies the agent but cmdline is NOT an MCP
    # server (eg the agent's own runtime, a user-spawned editor that
    # inherited the env). Must NOT be killed.
    non_mcp = _FakeProcess(
        pid=333,
        cmdline=["python3", "-m", "scitex_agent_container.cli"],
        environ=_agent_env("kappa"),
    )
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "kappa",
        process_iter=lambda: [non_mcp],
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


def test_process_with_no_env_visibility_is_skipped() -> None:
    # Arrange — restricted process whose environ() returned an empty
    # dict (psutil's AccessDenied surfaces here as ``{}``). Without an
    # env signal we have no agent-ownership evidence → must NOT kill.
    blind_proc = _FakeProcess(
        pid=444,
        cmdline=["bun", "run", "claude-code-telegrammer/server.ts"],
        environ={},
    )
    killer = _KillRecorder()
    # Act
    killed = kill_orphan_mcp_children(
        "lambda",
        process_iter=lambda: [blind_proc],
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert (killed, killer.calls) == ([], [])


# ---------------------------------------------------------------------------
# Real-kill path — calls killer with SIGKILL
# ---------------------------------------------------------------------------


def test_orphan_kill_invokes_kill_fn_with_sigkill() -> None:
    # Arrange
    orphan = _orphan_telegrammer(pid=555, name="mu")  # stx-allow: STX-NL001
    killer = _KillRecorder()
    # Act
    kill_orphan_mcp_children(
        "mu",
        process_iter=lambda: [orphan],
        kill_fn=killer,
        self_pid=1,
    )
    # Assert
    assert killer.calls == [(555, signal.SIGKILL)]  # stx-allow: STX-NL001


def test_kill_fn_raising_oserror_does_not_propagate() -> None:
    # Arrange — the orphan may have already exited between snapshot and
    # kill (ESRCH); the helper must swallow the OSError and still report
    # the PID as "killed" (we tried).
    def _raises_esrch(pid: int, sig: int) -> None:
        raise OSError("no such process")

    orphan = _orphan_telegrammer(pid=666, name="nu")  # stx-allow: STX-NL001
    # Act
    killed = kill_orphan_mcp_children(
        "nu",
        process_iter=lambda: [orphan],
        kill_fn=_raises_esrch,
        self_pid=1,
    )
    # Assert — PID is still in the list; the helper completes cleanly.
    assert killed == [666]  # stx-allow: STX-NL001
