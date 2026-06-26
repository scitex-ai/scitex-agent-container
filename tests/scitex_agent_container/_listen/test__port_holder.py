"""Tests for ``_listen/_port_holder.py`` — port-holder discovery +
self-heal (card ``sac-listen-restart-selfheal-cli``).

No-mocks discipline (PA-306 / STX-NM001-003): the external-tool seam
(``_run_subprocess``) and the discovery seams (``_probe_bound`` /
``_resolve_pids``) are swapped via a hand-rolled save/restore context
manager. The socket probe is exercised against a REAL loopback
``http.server`` / a real closed port — no MagicMock.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Iterator

from scitex_agent_container._listen import _port_holder as ph


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    saved = getattr(ph, name)
    setattr(ph, name, value)
    try:
        yield
    finally:
        setattr(ph, name, saved)


def _no_sleep(_secs: float) -> None:
    """No-op sleep seam."""


def _completed(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout, stderr=stderr
    )


class _TermRecorder:
    """Records (pid, force_kill) for each terminate call, returns escalated."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def __call__(self, pid: int, *, grace_secs: float, force_kill: bool) -> bool:
        self.calls.append((pid, force_kill))
        return force_kill


# ---------------------------------------------------------------------------
# port_is_bound — real loopback server (bound) + real closed port (free)
# ---------------------------------------------------------------------------


def test_port_is_bound_true_for_live_loopback_server() -> None:
    # Arrange — a real TCP server bound on an ephemeral loopback port.
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    _host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Act
    try:
        bound = ph.port_is_bound("127.0.0.1", port, timeout=1.0)
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert
    assert bound is True


def test_port_is_bound_false_for_closed_port() -> None:
    # Arrange — port 1 on loopback is not bound (connection refused).
    # Act
    bound = ph.port_is_bound("127.0.0.1", 1, timeout=0.2)
    # Assert
    assert bound is False


# ---------------------------------------------------------------------------
# port_holder_pids — lsof / ss / fuser parsing + fallback chain
# ---------------------------------------------------------------------------


def test_lsof_branch_parses_pid_lines() -> None:
    # Arrange — lsof -Fp emits one ``p<pid>`` line per holder.
    def _run(argv, **_kw):
        return _completed(stdout="p4242\nf7\n") if argv[0] == "lsof" else _completed()

    # Act
    with _swap("_run_subprocess", _run):
        pids = ph.port_holder_pids(7878)
    # Assert
    assert pids == [4242]


def test_falls_back_to_ss_when_lsof_missing() -> None:
    # Arrange — lsof raises FileNotFoundError; ss returns a pid= column.
    def _run(argv, **_kw):
        if argv[0] == "lsof":
            raise FileNotFoundError("lsof: not found")
        if argv[0] == "ss":
            return _completed(
                stdout='LISTEN 0 128 *:7878 *:* users:(("uv",pid=5151,fd=7))\n'
            )
        return _completed()

    # Act
    with _swap("_run_subprocess", _run):
        pids = ph.port_holder_pids(7878)
    # Assert
    assert pids == [5151]


def test_falls_back_to_fuser_when_lsof_and_ss_empty() -> None:
    # Arrange — lsof + ss yield nothing; fuser prints a bare PID.
    def _run(argv, **_kw):
        if argv[0] == "fuser":
            return _completed(stdout="", stderr="7878/tcp:        6262\n")
        return _completed()

    # Act
    with _swap("_run_subprocess", _run):
        pids = ph.port_holder_pids(7878)
    # Assert
    assert pids == [6262]


def test_returns_empty_when_no_tool_finds_holder() -> None:
    # Arrange — every tool returns nothing.
    with _swap("_run_subprocess", lambda *_a, **_kw: _completed()):
        # Act
        pids = ph.port_holder_pids(7878)
    # Assert
    assert pids == []


def test_own_pid_is_excluded_from_holders() -> None:
    # Arrange — lsof reports THIS process's PID; it must be filtered.
    import os

    def _run(argv, **_kw):
        if argv[0] == "lsof":
            return _completed(stdout=f"p{os.getpid()}\n")
        return _completed()

    # Act
    with _swap("_run_subprocess", _run):
        pids = ph.port_holder_pids(7878)
    # Assert
    assert pids == []


# ---------------------------------------------------------------------------
# clear_wedged_port_holders — the self-heal
# ---------------------------------------------------------------------------


def test_no_op_when_port_not_bound() -> None:
    # Arrange — port is free; heal must do nothing.
    term = _TermRecorder()
    with (
        _swap("_probe_bound", lambda _h, _p: False),
        _swap("_resolve_pids", lambda _p: [4242]),
    ):
        # Act
        result = ph.clear_wedged_port_holders(
            host="127.0.0.1",
            port=7878,
            grace_secs=1.0,
            force=True,
            terminate_fn=term,
            sleep_fn=_no_sleep,
            poll_interval=0.01,
        )
    # Assert
    assert result.killed == () and term.calls == []


def test_force_kills_holder_then_frees_port() -> None:
    # Arrange — bound before, free after the kill.
    term = _TermRecorder()
    states = iter([True, False])
    with (
        _swap("_probe_bound", lambda _h, _p: next(states, False)),
        _swap("_resolve_pids", lambda _p: [4242]),
    ):
        # Act
        result = ph.clear_wedged_port_holders(
            host="127.0.0.1",
            port=7878,
            grace_secs=1.0,
            force=True,
            terminate_fn=term,
            sleep_fn=_no_sleep,
            poll_interval=0.01,
        )
    # Assert — the remnant was killed and recorded.
    assert result.killed == (4242,) and result.error == ""


def test_loud_error_when_no_pid_resolves_but_bound() -> None:
    # Arrange — bound but no holder PID found.
    term = _TermRecorder()
    with (
        _swap("_probe_bound", lambda _h, _p: True),
        _swap("_resolve_pids", lambda _p: []),
    ):
        # Act
        result = ph.clear_wedged_port_holders(
            host="127.0.0.1",
            port=7878,
            grace_secs=1.0,
            force=True,
            terminate_fn=term,
            sleep_fn=_no_sleep,
            poll_interval=0.01,
        )
    # Assert
    assert "no holding PID could be resolved" in result.error


def test_loud_error_when_holder_survives_kill() -> None:
    # Arrange — port stays bound even after the kill (unkillable).
    term = _TermRecorder()
    with (
        _swap("_probe_bound", lambda _h, _p: True),
        _swap("_resolve_pids", lambda _p: [4242]),
    ):
        # Act
        result = ph.clear_wedged_port_holders(
            host="127.0.0.1",
            port=7878,
            grace_secs=1.0,
            force=True,
            terminate_fn=term,
            sleep_fn=_no_sleep,
            poll_interval=0.01,
        )
    # Assert
    assert "still held by PID 4242" in result.error


def test_without_force_uses_term_escalation_not_immediate_kill() -> None:
    # Arrange — force=False must pass force_kill=False to the terminator.
    term = _TermRecorder()
    states = iter([True, False])
    with (
        _swap("_probe_bound", lambda _h, _p: next(states, False)),
        _swap("_resolve_pids", lambda _p: [4242]),
    ):
        # Act
        ph.clear_wedged_port_holders(
            host="127.0.0.1",
            port=7878,
            grace_secs=1.0,
            force=False,
            terminate_fn=term,
            sleep_fn=_no_sleep,
            poll_interval=0.01,
        )
    # Assert
    assert term.calls == [(4242, False)]


# ---------------------------------------------------------------------------
# diagnose_unhealthy — names the REAL cause
# ---------------------------------------------------------------------------


def test_diagnose_names_wedged_pid_when_port_bound() -> None:
    # Arrange — port bound but not serving → up-but-not-serving.
    with (
        _swap("_probe_bound", lambda _h, _p: True),
        _swap("_resolve_pids", lambda _p: [7171]),
    ):
        # Act
        msg = ph.diagnose_unhealthy(
            host="127.0.0.1", port=7878, deadline_secs=30.0, health_path="/v1/health"
        )
    # Assert
    assert "NOT SERVING" in msg and "7171" in msg


def test_diagnose_reports_bind_failed_when_port_free() -> None:
    # Arrange — nothing bound after relaunch → bind failed.
    with _swap("_probe_bound", lambda _h, _p: False):
        # Act
        msg = ph.diagnose_unhealthy(
            host="127.0.0.1", port=7878, deadline_secs=30.0, health_path="/v1/health"
        )
    # Assert
    assert "bind failed" in msg
