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
5. Relaunch. If a systemd-user unit exists AND is enabled, prefer
   ``systemctl --user daemon-reload && systemctl --user restart
   sac-listen`` (handles a changed unit file). Otherwise direct
   ``sac listen`` subprocess spawn.
6. Verify on ``/v1/sac/health`` with a bounded poll loop. Fail loud
   if the new daemon doesn't come up.

Design calls locked by lead (msg ``c3cbf269f74c41e28ab37bb1e4be5cee``):

  (a) Bind resolution mirrors ``sac listen`` — caller passes the
      host+port that ``sac listen`` would resolve to; this module is
      bind-agnostic.
  (b) Prefer systemd — ``daemon-reload`` before ``restart`` so a
      changed unit file picks up.
  (c) Loud WARN on SIGKILL escalation — silent on clean TERM.
  (d) No ``sac dev systemd restart`` abstraction — strictly under
      ``sac listen restart``.

Pure-logic module — no click. Tests swap the module-level injection
points (``_kill``, ``_sleep``, ``_run_subprocess``, ``_http_get``)
via a save/restore context manager, no MagicMock.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_GRACE_SECS: float = 10.0
_POLL_INTERVAL_SECS: float = 0.2
_HEALTH_POLL_INTERVAL_SECS: float = 0.5
DEFAULT_HEALTH_DEADLINE_SECS: float = 30.0

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
    """Return the HTTP status for a GET, or -1 on any error.

    Uses stdlib ``urllib`` rather than ``httpx`` to keep the restart
    path dep-light — if uvicorn binds the port, urllib reaches it.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
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
    """

    ok: bool
    escalated_to_sigkill: bool
    had_prior_pidfile: bool
    prior_pid_alive: bool
    health_ok: bool
    took_systemd_path: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Pidfile + liveness helpers
# ---------------------------------------------------------------------------


def pidfile_path(port: int, lock_dir: Path) -> Path:
    """Mirror ``_single_instance._pid_file_path`` (port-scoped).

    Re-derived rather than imported to keep cli_pkg independent of
    the single-instance private API. Shape ``<lock_dir>/listen-<port>.pid``
    is the stable on-disk contract.
    """
    return lock_dir / f"listen-{port}.pid"


def read_pid_from_file(pid_file: Path) -> int | None:
    """Read the PID from ``pid_file``, or return ``None`` on absence /
    malformed / empty.

    Robust to a partially-written file and a stale pidfile from an
    earlier sac version that may carry junk.
    """
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
    """Return ``True`` iff a process with this PID currently exists.

    ``os.kill(pid, 0)`` is the canonical liveness probe.
    ``PermissionError`` means the PID is owned by another user —
    still alive from the perspective of "is the slot taken".
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

    Returns ``True`` iff SIGKILL escalation was used (CLI surfaces
    as LOUD WARN per design call (c)). Caller MUST verify the PID is
    actually dead before clearing the pidfile.

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

    Two-step check (file presence + ``systemctl --user is-enabled``)
    so we don't try to ``restart`` a unit that was copied in but
    never enabled — falls through to direct-spawn instead of
    surfacing a confusing systemd error.
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
    """Poll ``http://<host>:<port>/v1/sac/health`` until 200 or deadline.

    Endpoint shape fixed by ``_listen.server.health`` (route
    ``/v1/sac/health``, returns ``{"ok": true, "service":
    "sac-listen", "v": 1}``).
    """
    url = f"http://{host}:{port}/v1/sac/health"
    elapsed = 0.0
    while elapsed < deadline_secs:
        status = _http_get(url, timeout=2.0)
        if status == 200:
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

    ``host``/``port`` mirror ``sac listen``'s bind resolution per
    design call (a) — the CLI verb resolves them from the
    group-level ``--bind``. ``lock_dir`` matches
    ``_single_instance.default_lock_dir()`` in production. Never
    raises — every failure populates ``RestartResult.error``.
    """
    pid_file = pidfile_path(port, lock_dir)
    prior_pid = read_pid_from_file(pid_file)
    had_prior_pidfile = pid_file.is_file()
    prior_alive = prior_pid is not None and pid_alive(prior_pid)

    escalated = False
    if prior_pid is not None and prior_alive:
        escalated = _terminate_then_kill(
            prior_pid, grace_secs=grace_secs, force_kill=force
        )
        # Defence-in-depth: confirm the PID is actually dead before
        # touching the pidfile. If still alive after SIGKILL, bail
        # rather than clear the lock + let a second daemon start beside.
        _sleep(_POLL_INTERVAL_SECS)
        if pid_alive(prior_pid):
            return RestartResult(
                ok=False,
                escalated_to_sigkill=escalated,
                had_prior_pidfile=had_prior_pidfile,
                prior_pid_alive=prior_alive,
                health_ok=False,
                took_systemd_path=False,
                error=(
                    f"PID {prior_pid} survived SIGKILL — refusing to "
                    f"clear pidfile or relaunch. Inspect manually "
                    f"(zombie/uninterruptible state)."
                ),
            )

    try:
        pid_file.unlink(missing_ok=True)
    except OSError as exc:
        return RestartResult(
            ok=False,
            escalated_to_sigkill=escalated,
            had_prior_pidfile=had_prior_pidfile,
            prior_pid_alive=prior_alive,
            health_ok=False,
            took_systemd_path=False,
            error=f"failed to clear stale pidfile {pid_file}: {exc!r}",
        )

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
            return RestartResult(
                ok=False,
                escalated_to_sigkill=escalated,
                had_prior_pidfile=had_prior_pidfile,
                prior_pid_alive=prior_alive,
                health_ok=False,
                took_systemd_path=True,
                error=f"systemctl restart failed: {exc!r}",
            )
        if rc != 0:
            return RestartResult(
                ok=False,
                escalated_to_sigkill=escalated,
                had_prior_pidfile=had_prior_pidfile,
                prior_pid_alive=prior_alive,
                health_ok=False,
                took_systemd_path=True,
                error=f"systemctl restart exited rc={rc}",
            )
    else:
        argv = sac_listen_argv or ["sac", "listen"]
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
            return RestartResult(
                ok=False,
                escalated_to_sigkill=escalated,
                had_prior_pidfile=had_prior_pidfile,
                prior_pid_alive=prior_alive,
                health_ok=False,
                took_systemd_path=False,
                error=f"direct spawn failed: {exc!r}",
            )

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
        error=""
        if health_ok
        else (
            f"daemon did not respond 200 on /v1/sac/health within "
            f"{health_deadline_secs}s"
        ),
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
    "RestartResult",
    "format_escalation_warning",
    "pid_alive",
    "pidfile_path",
    "read_pid_from_file",
    "restart_listen",
    "systemd_unit_is_active",
    "wait_for_health",
]
