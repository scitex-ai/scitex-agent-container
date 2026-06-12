"""SSH ProxyJump tunnel manager for the apptainer provider lifecycle.

Operator directive 2026-06-08: an agent whose
``spec.claude.provider.endpoint`` is a :class:`TunneledEndpoint` must
stand up a local ``ssh -L`` forward at agent-start so the SDK can
talk to the provider through a stable ``http://localhost:<port>``
even though the upstream endpoint is only reachable through an
HPC-style ProxyJump bastion.

The manager owns the lifecycle:

* :meth:`TunnelManager.up` allocates a local port (ephemeral when the
  spec didn't pin one), spawns the supervisor child, writes a pidfile,
  polls the local port until a TCP connect succeeds, and returns the
  bound port. Polling closes a race where the SDK would otherwise
  try to talk to a still-binding tunnel and see a connection-refused.

* :meth:`TunnelManager.down` SIGTERMs the supervisor with a 5-second
  grace window, escalates to SIGKILL if needed, and removes the
  pidfile. Idempotent — calling it twice is a no-op the second time.

* :meth:`TunnelManager.is_alive` reads the pidfile, ``os.kill(pid,
  0)``-probes the supervisor, and TCP-connects to the local port to
  confirm the forward is actually up. All three must agree.

Test seam — ``supervisor_cmd``
------------------------------

The supervisor command defaults to ``[sys.executable, "-m",
"scitex_agent_container._network._tunnel_supervisor"]`` so the manager
spawns the real ssh wrapper in production. Tests pass a fake
``supervisor_cmd`` argv that opens a listening socket on the requested
local port (and never invokes ssh), exercising the bind-and-poll loop
without a real ssh setup.

Fail-loud contract
------------------

* Spec missing required fields (``jump_host``, ``target_host``,
  ``remote_port``) — :meth:`TunnelManager.up` raises
  :class:`TunnelUpError` BEFORE spawning anything.
* Supervisor never binds the requested port within ``wait_timeout_s``
  — SIGTERM the supervisor, raise :class:`TunnelUpError` with the
  concrete ``ssh -J <jump> <target>`` recipe so the operator can
  reproduce the failure outside sac.
* Pidfile / state-dir IO errors propagate (no silent skip); a stuck
  filesystem must not let sac believe a tunnel is up when it isn't.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..config._tunnel_types import TunnelSpec


class TunnelUpError(RuntimeError):
    """Raised when the local forward fails to become reachable in time.

    The message embeds the operator-actionable reproducer recipe
    (``ssh -J <jump> <target>``) so the operator can verify the
    underlying path works without sac in the loop.
    """


_PROBE_INTERVAL_S = 0.2  # 200ms — fast enough for a healthy bind, gentle on CPU.
_SIGKILL_GRACE_S = 5.0  # SIGTERM → SIGKILL escalation window.


def _pick_ephemeral_port() -> int:
    """Bind to port 0, read what the OS picked, release the socket.

    The narrow race between close and the supervisor binding is
    accepted: the supervisor immediately re-binds the same port, and
    on a healthy host the chance of collision in that microsecond is
    negligible. The alternative (passing the bound fd to the
    supervisor) would require an `ssh -L` extension that doesn't
    exist.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tcp_connect_ok(port: int, host: str = "127.0.0.1") -> bool:
    """Best-effort: TCP-connect to ``host:port`` with a short timeout."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        # The supervisor isn't bound yet; not an error worth raising,
        # just a "not ready" data point for the poll loop.
        return False


def _default_supervisor_cmd() -> list[str]:
    """Return the supervisor argv prefix used in production."""
    return [
        sys.executable,
        "-m",
        "scitex_agent_container._network._tunnel_supervisor",
    ]


def _validate_spec(spec: TunnelSpec) -> None:
    """Loud-reject an incomplete :class:`TunnelSpec` before spawning anything."""
    if not spec.jump_host:
        raise TunnelUpError("TunnelSpec.jump_host is required and must be non-empty.")
    if not spec.target_host:
        raise TunnelUpError("TunnelSpec.target_host is required and must be non-empty.")
    if not spec.remote_port or spec.remote_port < 1 or spec.remote_port > 65535:
        raise TunnelUpError(
            f"TunnelSpec.remote_port must be 1..65535, got {spec.remote_port!r}."
        )


def _read_pid(pidfile: Path) -> Optional[int]:
    """Read a pidfile or return None on any failure (file gone, garbage)."""
    try:
        text = pidfile.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` probe — True iff the process exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by a different uid — still alive.
        return True
    return True


class TunnelManager:
    """Lifecycle owner for one agent's SSH ProxyJump tunnel."""

    def __init__(
        self,
        spec: TunnelSpec,
        agent_name: str,
        state_dir: Path,
        supervisor_cmd: Optional[list[str]] = None,
    ) -> None:
        """Construct the manager (no side effects until :meth:`up`).

        Args:
            spec: The :class:`TunnelSpec` from the parsed
                provider endpoint. Validated lazily at :meth:`up`
                time so the manager can be constructed even when the
                operator is iterating on a partial spec via tests.
            agent_name: Used to name the pidfile so two agents with
                overlapping tunnels don't clobber each other's state.
            state_dir: The base state directory for sac on this host.
                The manager writes the pidfile to ``<state_dir>/
                tunnels/<agent_name>.pid``.
            supervisor_cmd: OPTIONAL argv prefix to spawn the
                supervisor. Default = ``[sys.executable, "-m",
                "scitex_agent_container._network._tunnel_supervisor"]``.
                Tests pass a fake here so the manager logic can be
                exercised without a real ssh setup.
        """
        self.spec = spec
        self.agent_name = agent_name
        self.state_dir = state_dir
        self._supervisor_cmd = (
            list(supervisor_cmd)
            if supervisor_cmd is not None
            else _default_supervisor_cmd()
        )
        self._local_port: int = spec.local_port if spec.local_port > 0 else 0
        self._proc: Optional[subprocess.Popen] = None

    @property
    def local_port(self) -> int:
        """Return the local-bound port. ``0`` before :meth:`up` runs."""
        return self._local_port

    @property
    def pidfile(self) -> Path:
        """Resolved pidfile path under ``state_dir/tunnels/<agent>.pid``."""
        return self.state_dir / "tunnels" / f"{self.agent_name}.pid"

    def up(self) -> int:
        """Stand up the tunnel; return the bound local port.

        Pidfile contents are written AFTER the supervisor is spawned
        so a stale file from a previous run never confuses a fresh
        boot. The poll loop closes the race where the SDK would
        otherwise see a connection-refused mid-boot.

        Raises:
            TunnelUpError: Spec incomplete; supervisor exits before
                binding; bind never succeeds within
                ``spec.wait_timeout_s``. The error message includes
                the ``ssh -J <jump> <target>`` recipe.
        """
        _validate_spec(self.spec)

        if self._local_port <= 0:
            self._local_port = _pick_ephemeral_port()

        # Resolve & build the supervisor argv. The supervisor accepts
        # the same flags the production module declares; tests pass a
        # fake that ignores the flags but accepts the same surface so
        # the manager stays seam-honest.
        argv = list(self._supervisor_cmd) + [
            "--jump",
            self.spec.jump_host,
            "--target",
            self.spec.target_host,
            "--remote-port",
            str(self.spec.remote_port),
            "--local-port",
            str(self._local_port),
            "--backoff",
            str(self.spec.respawn_backoff_s),
        ]
        for tok in self.spec.ssh_opts or []:
            argv.extend(["--ssh-opt", tok])

        # Make sure the state dir exists; mkdir parents idempotent.
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(argv)
        self._proc = proc
        # Write the pidfile after spawn so the path is always coherent
        # with a live supervisor (or absent when supervisor failed to
        # start).
        self.pidfile.write_text(str(proc.pid))

        deadline = time.monotonic() + max(1, self.spec.wait_timeout_s)
        while time.monotonic() < deadline:
            # Supervisor died before binding — clean up and fail loud.
            if proc.poll() is not None:
                rc = proc.returncode
                self._cleanup_pidfile()
                raise TunnelUpError(
                    f"tunnel supervisor exited rc={rc} before binding "
                    f"localhost:{self._local_port}. Reproduce: "
                    f"ssh -J {self.spec.jump_host} -p {self.spec.remote_port} "
                    f"{self.spec.target_host}"
                )
            if _tcp_connect_ok(self._local_port):
                return self._local_port
            time.sleep(_PROBE_INTERVAL_S)

        # Timeout — SIGTERM, clean up, raise. The supervisor's own
        # SIGTERM handler will forward to ssh and exit 0.
        self._signal_supervisor(signal.SIGTERM)
        self._cleanup_pidfile()
        raise TunnelUpError(
            f"tunnel did not bind localhost:{self._local_port} within "
            f"{self.spec.wait_timeout_s}s. Reproduce: "
            f"ssh -J {self.spec.jump_host} -p {self.spec.remote_port} "
            f"{self.spec.target_host}"
        )

    def down(self) -> None:
        """SIGTERM the supervisor with a SIGKILL escalation. Idempotent."""
        proc = self._proc
        pid = None
        if proc is not None and proc.poll() is None:
            pid = proc.pid
        else:
            pid = _read_pid(self.pidfile)
        if pid is None:
            self._cleanup_pidfile()
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._cleanup_pidfile()
            return
        # Poll for clean exit; escalate to SIGKILL on grace expiry.
        deadline = time.monotonic() + _SIGKILL_GRACE_S
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Reap the child if it's our own subprocess; otherwise let
        # the parent reap (we held only a pidfile reference).
        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        self._proc = None
        self._cleanup_pidfile()

    def is_alive(self) -> bool:
        """True iff pidfile exists AND pid responds AND port accepts connects."""
        pid = _read_pid(self.pidfile)
        if pid is None:
            return False
        if not _pid_alive(pid):
            return False
        if not self._local_port:
            return False
        return _tcp_connect_ok(self._local_port)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _signal_supervisor(self, sig: int) -> None:
        """Best-effort signal — ProcessLookupError is silently OK."""
        proc = self._proc
        if proc is None:
            pid = _read_pid(self.pidfile)
            if pid is None:
                return
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            return
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return

    def _cleanup_pidfile(self) -> None:
        """Remove the pidfile if present. Silent on missing."""
        try:
            self.pidfile.unlink()
        except FileNotFoundError:
            return


__all__ = ["TunnelManager", "TunnelUpError"]
