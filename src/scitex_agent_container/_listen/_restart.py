"""Atomic stop-clean-relaunch sequence for ``sac listen``.

Codifies the manual SIGTERM-hang recovery dance documented in
``scripts/systemd/README.md`` (PR #294) as a single verb:

1. Discover the running PID from the flock-backed pidfile at
   ``<lock_dir>/listen-<port>.pid``.
2. Send SIGTERM and poll for exit up to ``grace_secs`` (default 10s).
3. Escalate to SIGKILL if the daemon survives the deadline. Emit a
   LOUD warning to stderr per operator design call (c) — silence on
   a clean TERM exit, escalation must be visible.
4. Clear the stale pidfile (after verifying via ``kill -0`` that the
   named PID is actually dead — protects against killing a recycled
   PID).
5. **Self-heal a wedged port holder** (incident 2026-06-26, card
   ``sac-listen-restart-selfheal-cli``): after the pidfile path, if
   the port is STILL bound by an UNtracked remnant (a half-dead
   uvicorn that ``curl`` hangs on; pidfile may already be ``rm``-ed),
   discover + force-kill the holder. See :mod:`._port_holder` and
   :func:`clear_wedged_port_holders` for the mechanism — this codifies
   the manual ``pkill``/``setsid`` recovery.
6. Relaunch. If a systemd-user unit exists AND is enabled, prefer
   ``systemctl --user daemon-reload && systemctl --user restart
   sac-listen`` (handles a changed unit file). Otherwise direct
   ``sac listen`` subprocess spawn.
7. Verify on ``/v1/health`` with a bounded poll loop. **Fail loud**
   if the daemon doesn't come up — the error names the REAL cause
   (``port still held by PID X`` / ``bind failed``), not a generic
   "did not respond". Aligns with ``_lifecycle/_bind_watchdog.py``
   (PR #469), which logs the same fail-loud ERROR for an
   up-but-not-serving daemon — both close the silent-outage door from
   opposite ends.

Design calls locked by lead (msg ``c3cbf269f74c41e28ab37bb1e4be5cee``):
(a) bind resolution mirrors ``sac listen`` (caller passes host+port;
this module is bind-agnostic); (b) prefer systemd — ``daemon-reload``
before ``restart``; (c) loud WARN on SIGKILL escalation, silent on
clean TERM; (d) no ``sac dev systemd restart`` abstraction — strictly
under ``sac listen restart``.

Pure-logic module — no click. Tests swap the module-level seams
(``_kill``, ``_sleep``, ``_run_subprocess``, ``_http_get``) here, and
the port-holder seams on :mod:`._port_holder`, via a save/restore
context manager, no MagicMock.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .._sac_binary import SacBinaryNotFoundError, sac_binary
from ._port_holder import (
    PortHealResult,
    clear_wedged_port_holders,
    diagnose_unhealthy,
    port_holder_pids,
    port_is_bound,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_GRACE_SECS: float = 10.0
_POLL_INTERVAL_SECS: float = 0.2
_HEALTH_POLL_INTERVAL_SECS: float = 0.5
DEFAULT_HEALTH_DEADLINE_SECS: float = 30.0

# The ONLY registered liveness route. ``_listen.server`` registers the
# ``health`` handler at ``/v1/health`` (see its routing table) — there
# is no ``/v1/sac/health`` route. The probe previously hit the latter
# and only "worked" because ``wait_for_health`` treats any HTTP
# response (incl. the 404) as alive; the URL was wrong + brittle.
# Single source of truth so the probe + the bind-watchdog
# (``_lifecycle/_bind_watchdog.py``) and tests can't drift.
HEALTH_PATH: str = "/v1/health"

DEFAULT_SYSTEMD_UNIT_PATH: Path = (
    Path.home() / ".config" / "systemd" / "user" / "sac-listen.service"
)


# ---------------------------------------------------------------------------
# Test seams (module-level swappable callables, NO MagicMock).
# Tests reassign via save/restore mirroring the pattern in
# ``cli_pkg/image_group._load_apptainer``.
# ---------------------------------------------------------------------------

_kill: Callable[[int, int], None] = os.kill
_sleep: Callable[[float], None] = time.sleep
_run_subprocess: Callable[..., subprocess.CompletedProcess] = subprocess.run


def _default_http_get(url: str, timeout: float) -> int:
    """Return the HTTP status for a GET, or -1 if the server is *down*.

    Stdlib ``urllib`` (dep-light). An HTTP error *response*
    (401/403/404/5xx) is NOT a failure — it PROVES a daemon is bound
    and answering, so we surface the real ``.code`` rather than
    collapse it to ``-1`` (the bearer-auth false-negative fix, PR #463,
    card ``sac-listen-restart-healthcheck-bearer``). Only a transport
    failure (refused / DNS / timeout) returns ``-1``.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx — the daemon answered. Surface the real status so the
        # liveness predicate can see "alive but auth-gated" (401/403).
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        # Transport-level failure (refused / timeout / DNS) — down.
        return -1


_http_get: Callable[[str, float], int] = _default_http_get


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartResult:
    """Outcome of a ``restart_listen`` invocation.

    Fields enumerate every observable: success / escalation /
    pre-state / health / which relaunch path / error message.
    The CLI verb surfaces ``escalated_to_sigkill`` as a LOUD WARN
    per design call (c).

    ``port_holders_killed`` records any UNtracked PID(s) the self-heal
    force-killed off the port (the wedged-remnant / "curl hangs"
    case) — empty when only the pidfile-tracked daemon was stopped.
    """

    ok: bool
    escalated_to_sigkill: bool
    had_prior_pidfile: bool
    prior_pid_alive: bool
    health_ok: bool
    took_systemd_path: bool
    error: str = ""
    port_holders_killed: tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# Pidfile + liveness helpers
# ---------------------------------------------------------------------------


def pidfile_path(port: int, lock_dir: Path) -> Path:
    """Mirror ``_single_instance._pid_file_path`` (port-scoped).

    Re-derived (not imported) to keep cli_pkg independent of the
    single-instance private API. ``<lock_dir>/listen-<port>.pid`` is
    the stable on-disk contract.
    """
    return lock_dir / f"listen-{port}.pid"


def read_pid_from_file(pid_file: Path) -> int | None:
    """Read the PID from ``pid_file``, or ``None`` on absent / malformed
    / empty (robust to a partial write or junk from an older sac)."""
    try:
        content = pid_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not content:
        return None
    first_line = content.split("\n", 1)[0].strip()
    try:
        return int(first_line)
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    """Return ``True`` iff a process with this PID exists.

    ``os.kill(pid, 0)`` is the canonical liveness probe;
    ``PermissionError`` (other-user PID) still counts as alive.
    """
    try:
        _kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Stop sequence
# ---------------------------------------------------------------------------


def _terminate_then_kill(
    pid: int,
    *,
    grace_secs: float,
    force_kill: bool,
) -> bool:
    """Send SIGTERM, poll for exit up to ``grace_secs``, escalate to
    SIGKILL on timeout.

    Returns ``True`` iff SIGKILL escalation was used (CLI surfaces as
    LOUD WARN per (c)). Caller MUST verify the PID is dead before
    clearing the pidfile.

    ``force_kill=True`` skips the TERM step. CLI exposes as
    ``--force`` for an operator who already knows the daemon hangs.
    """
    if force_kill:
        try:
            _kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    try:
        _kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False

    deadline = grace_secs
    while deadline > 0:
        _sleep(_POLL_INTERVAL_SECS)
        deadline -= _POLL_INTERVAL_SECS
        if not pid_alive(pid):
            return False

    try:
        _kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


# ---------------------------------------------------------------------------
# Relaunch path detection
# ---------------------------------------------------------------------------


def systemd_unit_is_active(
    unit_path: Path = DEFAULT_SYSTEMD_UNIT_PATH,
    *,
    unit_name: str = "sac-listen.service",
) -> bool:
    """Return ``True`` iff a ``sac-listen.service`` user unit is
    installed AND enabled.

    Two-step (file presence + ``systemctl --user is-enabled``) so we
    don't ``restart`` a copied-but-never-enabled unit — falls through
    to direct-spawn instead of a confusing systemd error.
    """
    if not unit_path.is_file():
        return False
    try:
        result = _run_subprocess(
            ["systemctl", "--user", "is-enabled", unit_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def wait_for_health(
    *,
    host: str,
    port: int,
    deadline_secs: float,
) -> bool:
    """Poll ``http://<host>:<port>/v1/health`` until the daemon is
    *alive* or the deadline elapses.

    "Alive" means the daemon answered with *any* HTTP status — a 200
    from the unauthenticated health route, but ALSO a 401/403 from
    :class:`~_listen.auth.BearerAuthMiddleware` (the daemon is up and
    auth-gating) or even a 404. The only "down" signal is a transport
    failure (connection refused / timeout), which ``_http_get``
    reports as ``-1``.

    Rationale (card ``sac-listen-restart-healthcheck-bearer``, PR #463):
    a restart must NEVER SIGKILL + abort against a daemon that is
    demonstrably answering. Gating liveness on ``status == 200`` made
    a later bearer-auth change re-classify a live, 401-answering
    daemon as "down" — a fail-quiet that destroyed a healthy process.
    Treating "got an HTTP response" as alive is auth-change-proof.
    ``HEALTH_PATH`` (``/v1/health``) is the only route ``server.py``
    registers.
    """
    url = f"http://{host}:{port}{HEALTH_PATH}"
    elapsed = 0.0
    while elapsed < deadline_secs:
        status = _http_get(url, timeout=2.0)
        # Any HTTP response (positive status) proves the daemon is
        # bound and answering — incl. 401/403 under bearer auth.
        # Only ``-1`` (transport failure) means "not up yet".
        if status > 0:
            return True
        _sleep(_HEALTH_POLL_INTERVAL_SECS)
        elapsed += _HEALTH_POLL_INTERVAL_SECS
    return False


# ---------------------------------------------------------------------------
# Top-level: restart_listen
# ---------------------------------------------------------------------------


def restart_listen(
    *,
    host: str,
    port: int,
    lock_dir: Path,
    grace_secs: float = DEFAULT_GRACE_SECS,
    force: bool = False,
    health_deadline_secs: float = DEFAULT_HEALTH_DEADLINE_SECS,
    systemd_unit_path: Path = DEFAULT_SYSTEMD_UNIT_PATH,
    sac_listen_argv: list[str] | None = None,
) -> RestartResult:
    """Execute the full stop-clean-relaunch sequence.

    ``host``/``port`` mirror ``sac listen``'s bind resolution per (a);
    ``lock_dir`` matches ``_single_instance.default_lock_dir()``. Never
    raises — every failure populates ``RestartResult.error``.
    """
    pid_file = pidfile_path(port, lock_dir)
    prior_pid = read_pid_from_file(pid_file)
    had_prior_pidfile = pid_file.is_file()
    prior_alive = prior_pid is not None and pid_alive(prior_pid)

    escalated = False
    port_holders_killed: tuple[int, ...] = ()

    def _fail(*, error: str, took_systemd_path: bool = False) -> RestartResult:
        """Failure result capturing pre-state + self-heal progress so
        far. De-dupes the early-return branches below."""
        return RestartResult(
            ok=False,
            escalated_to_sigkill=escalated,
            had_prior_pidfile=had_prior_pidfile,
            prior_pid_alive=prior_alive,
            health_ok=False,
            took_systemd_path=took_systemd_path,
            error=error,
            port_holders_killed=port_holders_killed,
        )

    if prior_pid is not None and prior_alive:
        escalated = _terminate_then_kill(
            prior_pid, grace_secs=grace_secs, force_kill=force
        )
        # Defence-in-depth: confirm the PID is actually dead before
        # touching the pidfile. If still alive after SIGKILL, bail
        # rather than clear the lock + let a second daemon start beside.
        _sleep(_POLL_INTERVAL_SECS)
        if pid_alive(prior_pid):
            return _fail(
                error=(
                    f"PID {prior_pid} survived SIGKILL — refusing to "
                    f"clear pidfile or relaunch. Inspect manually "
                    f"(zombie/uninterruptible state)."
                )
            )

    try:
        pid_file.unlink(missing_ok=True)
    except OSError as exc:
        return _fail(error=f"failed to clear stale pidfile {pid_file}: {exc!r}")

    # Self-heal a WEDGED port holder the pidfile never named (the
    # "curl hangs forever" remnant) before relaunch so the new daemon
    # doesn't hit EADDRINUSE. terminate/sleep are passed in so the
    # escalation seams stay owned here (and avoid a circular import).
    heal = clear_wedged_port_holders(
        host=host,
        port=port,
        grace_secs=grace_secs,
        force=force,
        terminate_fn=_terminate_then_kill,
        sleep_fn=_sleep,
        poll_interval=_POLL_INTERVAL_SECS,
    )
    port_holders_killed = heal.killed
    if heal.error:
        return _fail(error=heal.error)

    use_systemd = systemd_unit_is_active(systemd_unit_path)
    if use_systemd:
        try:
            _run_subprocess(
                ["systemctl", "--user", "daemon-reload"],
                check=False,
                timeout=10.0,
            )
            rc = _run_subprocess(
                ["systemctl", "--user", "restart", "sac-listen.service"],
                check=False,
                timeout=15.0,
            ).returncode
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return _fail(
                error=f"systemctl restart failed: {exc!r}",
                took_systemd_path=True,
            )
        if rc != 0:
            return _fail(
                error=f"systemctl restart exited rc={rc}",
                took_systemd_path=True,
            )
    else:
        try:
            argv = sac_listen_argv or [sac_binary(), "listen"]
        except SacBinaryNotFoundError as exc:
            return _fail(error=f"cannot resolve sac binary: {exc}")
        try:
            _run_subprocess(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                timeout=2.0,
            )
        except subprocess.TimeoutExpired:
            # EXPECTED — the spawned daemon never exits.
            pass
        except (FileNotFoundError, OSError) as exc:
            return _fail(error=f"direct spawn failed: {exc!r}")

    health_ok = wait_for_health(
        host=host, port=port, deadline_secs=health_deadline_secs
    )
    return RestartResult(
        ok=health_ok,
        escalated_to_sigkill=escalated,
        had_prior_pidfile=had_prior_pidfile,
        prior_pid_alive=prior_alive,
        health_ok=health_ok,
        took_systemd_path=use_systemd,
        error=(
            ""
            if health_ok
            else diagnose_unhealthy(
                host=host,
                port=port,
                deadline_secs=health_deadline_secs,
                health_path=HEALTH_PATH,
            )
        ),
        port_holders_killed=port_holders_killed,
    )


# ---------------------------------------------------------------------------
# Loud-WARN formatter — used by the CLI to surface escalation per (c)
# ---------------------------------------------------------------------------


def format_escalation_warning(grace_secs: float) -> str:
    """The exact stderr line the CLI emits when SIGKILL was used.

    Free-standing helper so tests can pin the wire string + so the
    CLI verb stays a thin wrapper.
    """
    return (
        f"WARN: escalated to SIGKILL after {grace_secs}s; "
        f"daemon hung on SIGTERM (likely in-flight SSE shutdown). "
        f"See scripts/systemd/README.md for the manual recovery."
    )


__all__ = [
    "DEFAULT_GRACE_SECS",
    "DEFAULT_HEALTH_DEADLINE_SECS",
    "DEFAULT_SYSTEMD_UNIT_PATH",
    "HEALTH_PATH",
    "PortHealResult",
    "RestartResult",
    "clear_wedged_port_holders",
    "diagnose_unhealthy",
    "format_escalation_warning",
    "pid_alive",
    "pidfile_path",
    "port_holder_pids",
    "port_is_bound",
    "read_pid_from_file",
    "restart_listen",
    "systemd_unit_is_active",
    "wait_for_health",
]
