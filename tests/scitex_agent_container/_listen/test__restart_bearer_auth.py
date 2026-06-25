"""Regression tests for the bearer-auth health-probe false-negative.

Card ``sac-listen-restart-healthcheck-bearer`` (P1): a bearer-auth
merge began gating the listen health endpoint, so the restart's
unauthenticated post-relaunch probe got a 401 — which the probe used
to read as "daemon down", SIGKILLing + aborting against a daemon that
was demonstrably ALIVE (it answered 401).

The fix: ``_default_http_get`` surfaces the real HTTP status from an
``HTTPError`` (401/403/404/5xx) instead of collapsing it to ``-1``,
and ``wait_for_health`` treats *any* HTTP response (positive status)
as "alive" — only a transport failure (``-1``) means "down".

PA-306 + STX-NM001-003 compliant: no MagicMock / no monkeypatch. The
module-level seams are swapped via a hand-rolled save/restore context
manager; the transport test uses a REAL loopback ``http.server``.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from scitex_agent_container._listen import _restart as restart_mod
from scitex_agent_container._listen._restart import restart_listen


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    """Replace ``restart_mod.<name>`` for the duration of the block."""
    saved = getattr(restart_mod, name)
    setattr(restart_mod, name, value)
    try:
        yield
    finally:
        setattr(restart_mod, name, saved)


def _no_sleep(_secs: float) -> None:
    """Recorded fake: no-op sleep, makes the poll loop instant."""


class _KillRecorder:
    """Hand-rolled fake for ``os.kill`` — kill(pid, 0) reports the pid
    dead so the relaunch path proceeds to the health probe.
    """

    def __init__(self, *, alive_script: list[bool]) -> None:
        self.calls: list[tuple[int, int]] = []
        self._script = list(alive_script)
        self._last_alive = alive_script[-1] if alive_script else False

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if sig == 0:
            if self._script:
                alive = self._script.pop(0)
                self._last_alive = alive
            else:
                alive = self._last_alive
            if not alive:
                raise ProcessLookupError(pid)


class _SubprocessRecorder:
    """Hand-rolled fake for ``subprocess.run`` — records argv + rc."""

    def __init__(self, *, returncodes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._rcs = list(returncodes) if returncodes else []

    def __call__(self, *args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args", []))
        self.calls.append(argv)
        rc = self._rcs.pop(0) if self._rcs else 0
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout="", stderr=""
        )


class _HttpRecorder:
    """Hand-rolled fake for the http-get seam — scripted statuses."""

    def __init__(self, *, statuses: list[int]) -> None:
        self.calls: list[tuple[str, float]] = []
        self._statuses = list(statuses)

    def __call__(self, url: str, timeout: float) -> int:
        self.calls.append((url, timeout))
        if not self._statuses:
            return -1
        return self._statuses.pop(0)


# ---------------------------------------------------------------------------
# Full restart flow — a 401-answering relaunch is "up", not aborted.
# ---------------------------------------------------------------------------


def test_restart_treats_401_relaunch_as_ok(tmp_path: Path) -> None:
    # Arrange — relaunched daemon answers the unauthenticated probe with
    # 401 (bearer-auth gate). It is ALIVE, so restart must report ok.
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[401])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            health_deadline_secs=0.5,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert — 401 = alive = restart succeeded (no false-negative abort).
    assert result.ok is True


def test_restart_401_carries_no_error_message(tmp_path: Path) -> None:
    # Arrange — same 401-answering relaunch; the error field must be
    # empty (a 401 is liveness, not a failure to surface to the operator).
    pid_file = tmp_path / "listen-7878.pid"
    pid_file.write_text("12345\n")
    kill = _KillRecorder(alive_script=[False])
    subproc = _SubprocessRecorder(returncodes=[0])
    http = _HttpRecorder(statuses=[401])
    # Act
    with (
        _swap("_kill", kill),
        _swap("_sleep", _no_sleep),
        _swap("_run_subprocess", subproc),
        _swap("_http_get", http),
    ):
        result = restart_listen(
            host="127.0.0.1",
            port=7878,
            lock_dir=tmp_path,
            health_deadline_secs=0.5,
            systemd_unit_path=tmp_path / "absent.service",
            sac_listen_argv=["echo", "stub"],
        )
    # Assert
    assert result.error == ""


# ---------------------------------------------------------------------------
# Transport layer — a real 401-serving loopback server, no mocks.
# ---------------------------------------------------------------------------


def test_default_http_get_returns_401_status_not_minus_one() -> None:
    # Arrange — a real loopback HTTP server that always 401s, mirroring
    # the bearer-auth gate. No mock/monkeypatch: a genuine TCP server.
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"missing bearer token"}')

        def log_message(self, *_args):  # silence test noise
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Act
    try:
        status = restart_mod._default_http_get(
            f"http://{host}:{port}/v1/sac/health", timeout=2.0
        )
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
    # Assert — the real 401 status surfaces, never collapsed to -1.
    assert status == 401
