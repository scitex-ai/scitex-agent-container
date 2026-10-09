"""Ownership proof for the channel inbox dispatcher pidfile.

Companion to :mod:`._channel_inbox_dispatcher_lifecycle` (per-file
512-line cap): everything that decides whether a PID recorded in
``channel-inbox-dispatcher.pid`` may be signalled lives here, so the
lifecycle module stays a thin start/stop orchestrator.

Decision table for :func:`stop_inbox_dispatcher_detailed` — first match
wins. Deleting a pidfile pointer never signals a process; signalling
requires proof:

  pidfile absent/unparseable ......... ``absent`` (False, nothing to do)
  recorded pid demonstrably dead ..... ``reaped-stale-pidfile`` (True)
  ownership proven ................... ``stopped`` (True, SIGTERM→SIGKILL)
  mismatch + incarnation row ended .... ``incarnation-superseded`` (True)
  mismatch + row active + row pid dead  ``reaped-stale-pidfile`` (True,
                                        row marked ``stale-cleared``)
  mismatch + live pid, row live ....... ``live-foreign-owner`` (False)
  mismatch + pid/row unverifiable ..... ``owner-unverified`` (False)

Only a live foreign owner — or an owner no evidence can verify —
refuses. Every other mismatch shape deletes the stale pointer and
proceeds. A prior-version (pre-stamp, legacy-module) owner is accepted
with a warning, never refused: rolling upgrades must not strand a
running dispatcher.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import scitex_logging as slogging

from ._channel_inbox_dispatcher_pidfile import (
    PIDFILE_SCHEMA_VERSION,
    PIDFILE_TTL_S,
    DispatcherPidRecord,
    pidfile_expired,
    read_dispatcher_pidfile,
    touch_dispatcher_pidfile,
    write_dispatcher_pidfile,
)
from ._inbox_sidecar_reconcile import CURRENT_MODULE, LEGACY_MODULE

log = slogging.getLogger(__name__)

#: The stop outcome codes. ``stopped`` / ``absent`` / ``reaped-*`` /
#: ``superseded`` all mean "proceed" (``stopped=True``); only the two
#: ``refused-*`` codes refuse, so lead-side handling can switch on the
#: code instead of parsing a log line.
STOP_CODE_STOPPED = "stopped"
STOP_CODE_ABSENT = "absent"
STOP_CODE_REAPED = "reaped-stale-pidfile"
STOP_CODE_SUPERSEDED = "incarnation-superseded"
STOP_CODE_REFUSED_FOREIGN = "live-foreign-owner"
STOP_CODE_REFUSED_UNVERIFIED = "owner-unverified"

_STOP_GRACE_S = 5.0


@dataclass(frozen=True)
class DispatcherStopOutcome:
    """Structured stop verdict for lead-side handling (see codes above)."""

    stopped: bool
    code: str
    detail: str
    pid: int | None
    expired: bool = False


def canonical_config_path(path: str) -> str:
    """Normalise a config-path spelling for ownership comparison.

    Mirrors :func:`._inbox_sidecar_reconcile._canonical`: ``~`` expanded
    and symlinks resolved, so an authority spelling (registry) and a
    live spelling (``/proc`` cmdline) compare equal when they name one
    file. ``strict=False`` — a path deleted mid-stop must not raise.
    """
    return str(Path(path).expanduser().resolve(strict=False))


def _argv_value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _owns_dispatcher_process(
    pid: int,
    *,
    name: str,
    config_path: str,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Prove a PID is this bridge for this agent and authored spec.

    The ``--config-path`` argv spelling is compared by realpath, so a
    registry spelling and a live spelling of one file match. A
    prior-version (legacy-module) owner is accepted — the caller logs
    the upgrade warning — because refusing it strands a dispatcher
    across a rolling upgrade.
    """
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [part.decode(errors="replace") for part in raw if part]
    if CURRENT_MODULE not in argv and LEGACY_MODULE not in argv:
        return False
    if _argv_value(argv, "--name") != name:
        return False
    authored = _argv_value(argv, "--config-path")
    if authored is None:
        return False
    try:
        return canonical_config_path(authored) == canonical_config_path(
            config_path
        )
    except OSError:
        return False


def pid_alive(pid: int) -> bool | None:
    """Resolve PID existence without treating unknown errors as death.

    Mirrors :func:`._lifecycle._stale_lease._pid_alive`: ``os.kill(pid,
    0)`` raises ``ProcessLookupError`` for a dead PID and
    ``PermissionError`` for a live PID owned by another uid (still proof
    of life). Any other ``OSError`` is indeterminate — ``None`` — and
    must never authorise retiring an ownership record.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def registry_known_config_paths(name: str) -> list[str]:
    """Registry-known spellings of an agent's spec path (never raises).

    The registry row holds the authority spelling; the resolved
    absolute spelling covers symlink drift between what was recorded
    and what a live process was launched with.
    """
    try:
        from .._state.registry import Registry

        entry = Registry().get(name)
    except Exception:  # stx-allow: fallback (unknown spellings, not a stop fault)
        return []
    raw = (entry or {}).get("config")
    if not raw:
        return []
    spellings = [str(raw)]
    try:
        resolved = str(Path(str(raw)).expanduser().resolve(strict=False))
    except OSError:
        return spellings
    if resolved not in spellings:
        spellings.append(resolved)
    return spellings


def read_incarnation_row(name: str) -> dict[str, Any] | None:
    """Newest local incarnation row for ``name`` (never raises).

    Active or ended — an ended row is itself the verdict ("stopped or
    superseded, proceed"). ``None`` means unknown (no row, or the store
    unreachable), which must refuse, never proceed.
    """
    try:
        from .._state.state_store_instances import (
            last_local_instance_for_name,
        )

        return last_local_instance_for_name(name)
    except Exception:  # stx-allow: fallback (unknown row refuses, never proceeds)
        return None


def close_incarnation_row(row_id: str, reason: str = "stale-cleared") -> bool:
    """Mark one incarnation row ended (never raises; True iff ended)."""
    try:
        from .._state.state_store import record_instance_stop

        return bool(record_instance_stop(str(row_id), exit_reason=reason))
    except Exception:  # stx-allow: fallback (a failed UPDATE retries next stop)
        return False


def _row_is_ended(row: Mapping[str, Any]) -> bool:
    return row.get("ended_at") is not None


def _row_pid_dead(
    row: Mapping[str, Any],
    pid_alive_fn: Callable[[int], bool | None],
) -> bool:
    """Per-row proof of deadness (mirrors the stale-lease contract).

    A NULL/unparseable row pid is NEVER proof — without a PID there is
    no per-row evidence, so the row is left alone however dead the
    runtime looks.
    """
    pid = row.get("pid")
    if pid is None or isinstance(pid, bool):
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    return pid_alive_fn(pid_int) is False


def _is_owned(
    pid: int,
    name: str,
    spellings: list[str],
    owns: Callable[..., bool],
) -> bool:
    """Ownership across every known config-path spelling."""
    return any(
        owns(pid, name=name, config_path=candidate) for candidate in spellings
    )


def _signal_owned(
    pid_path: Path,
    record: DispatcherPidRecord,
    name: str,
    spellings: list[str],
    *,
    kill: Callable[[int, int], None],
    sleep: Callable[[float], None],
    owns: Callable[..., bool],
    expired: bool,
) -> DispatcherStopOutcome:
    """SIGTERM a proven owner, escalating to SIGKILL on grace-timeout."""
    if record.legacy or record.module not in (None, CURRENT_MODULE):
        log.warning(
            "channel inbox dispatcher PID %s for %s is a prior-version "
            "owner (module=%r version=%r); accepting with warning",
            record.pid,
            name,
            record.module,
            record.sac_version,
        )
    try:
        kill(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=True,
            code=STOP_CODE_REAPED,
            detail=f"dispatcher PID {record.pid} already exited",
            pid=record.pid,
            expired=expired,
        )
    except OSError as exc:
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=False,
            code=STOP_CODE_REFUSED_UNVERIFIED,
            detail=f"could not signal dispatcher PID {record.pid}: {exc}",
            pid=record.pid,
            expired=expired,
        )
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _is_owned(record.pid, name, spellings, owns):
            pid_path.unlink(missing_ok=True)
            return DispatcherStopOutcome(
                stopped=True,
                code=STOP_CODE_STOPPED,
                detail=f"dispatcher PID {record.pid} exited on SIGTERM",
                pid=record.pid,
                expired=expired,
            )
        sleep(0.05)
    if _is_owned(record.pid, name, spellings, owns):
        try:
            kill(record.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    pid_path.unlink(missing_ok=True)
    return DispatcherStopOutcome(
        stopped=True,
        code=STOP_CODE_STOPPED,
        detail=f"dispatcher PID {record.pid} stopped",
        pid=record.pid,
        expired=expired,
    )


def _read_row_for_stop(
    name: str,
    instance_reader: Callable[[str], dict[str, Any] | None] | None,
) -> dict[str, Any] | None:
    """Best-effort incarnation row (None when unknown; never raises)."""
    reader = instance_reader or read_incarnation_row
    try:
        row = reader(name)
    except Exception:  # stx-allow: fallback (unknown row refuses, never proceeds)
        return None
    return row if isinstance(row, Mapping) else None


def _mark_stale_row(
    row: Mapping[str, Any],
    stop_writer: Callable[[str, str], bool] | None,
    pid_alive_fn: Callable[[int], bool | None],
) -> bool:
    """Close a demonstrably stale row (never raises; True iff marked)."""
    writer = stop_writer or close_incarnation_row
    row_id = row.get("id")
    if not row_id or not _row_pid_dead(row, pid_alive_fn):
        return False
    try:
        return bool(writer(str(row_id), "stale-cleared"))
    except Exception:  # stx-allow: fallback (retry next stop)
        return False


def stop_inbox_dispatcher_detailed(
    config: Any,
    *,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    state_dir: Path | None = None,
    owns: Callable[..., bool] = _owns_dispatcher_process,
    instance_reader: Callable[[str], dict[str, Any] | None] | None = None,
    stop_writer: Callable[[str, str], bool] | None = None,
    pid_alive_fn: Callable[[int], bool | None] = pid_alive,
    known_config_paths: Iterable[str] | None = None,
    ttl_s: float = PIDFILE_TTL_S,
    pid_path: Path | None = None,
) -> DispatcherStopOutcome:
    """Stop only the identity-proven bridge recorded for this agent.

    ``config`` carries ``name`` / ``config_path``; ``pid_path`` (or
    ``state_dir``) locates the pidfile. On ownership mismatch the DB
    incarnation row is consulted before refusing (see module docstring
    for the decision table). Never raises for store/registry failures —
    those degrade to ``owner-unverified`` refusal, never to a signal.
    """
    name = str(getattr(config, "name", "") or "")
    config_path = str(getattr(config, "config_path", "") or "")
    if pid_path is None:
        if state_dir is not None:
            pid_path = state_dir / "channel-inbox-dispatcher.pid"
        else:
            from .tui_session import state_dir_for_config

            pid_path = state_dir_for_config(config) / "channel-inbox-dispatcher.pid"
    record = read_dispatcher_pidfile(pid_path)
    if record is None:
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=False,
            code=STOP_CODE_ABSENT,
            detail=f"no parseable dispatcher pidfile for {name!r}",
            pid=None,
        )
    expired = pidfile_expired(pid_path, ttl_s)
    spellings = [config_path]
    if known_config_paths is None:
        spellings += registry_known_config_paths(name)
    else:
        spellings += [str(item) for item in known_config_paths]
    if _is_owned(record.pid, name, spellings, owns):
        return _signal_owned(
            pid_path,
            record,
            name,
            spellings,
            kill=kill,
            sleep=sleep,
            owns=owns,
            expired=expired,
        )
    if pid_alive_fn(record.pid) is False:
        row = _read_row_for_stop(name, instance_reader)
        marked = (
            _mark_stale_row(row, stop_writer, pid_alive_fn)
            if row is not None and not _row_is_ended(row)
            else False
        )
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=True,
            code=STOP_CODE_REAPED,
            detail=(
                f"dispatcher PID {record.pid} dead; pidfile reaped"
                + ("; incarnation row marked stale-cleared" if marked else "")
            ),
            pid=record.pid,
            expired=expired,
        )
    row = _read_row_for_stop(name, instance_reader)
    if row is not None and _row_is_ended(row):
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=True,
            code=STOP_CODE_SUPERSEDED,
            detail=(
                f"dispatcher PID {record.pid} not owned by {name!r} but its "
                f"incarnation row {row.get('id')} is ended "
                f"({row.get('exit_reason')}); pidfile reaped"
            ),
            pid=record.pid,
            expired=expired,
        )
    if row is not None and not _row_is_ended(row):
        if _row_pid_dead(row, pid_alive_fn):
            marked = _mark_stale_row(row, stop_writer, pid_alive_fn)
            pid_path.unlink(missing_ok=True)
            return DispatcherStopOutcome(
                stopped=True,
                code=STOP_CODE_REAPED,
                detail=(
                    f"dispatcher PID {record.pid} not owned by {name!r} but "
                    f"its incarnation row {row.get('id')} is stale"
                    + (" (marked)" if marked else "")
                    + "; pidfile reaped"
                ),
                pid=record.pid,
                expired=expired,
            )
        log.warning(
            "channel inbox dispatcher PID %s is not owned by %s "
            "(live foreign owner%s); not signalling",
            record.pid,
            name,
            "; pidfile expired" if expired else "",
        )
        pid_path.unlink(missing_ok=True)
        return DispatcherStopOutcome(
            stopped=False,
            code=STOP_CODE_REFUSED_FOREIGN,
            detail=(
                f"dispatcher PID {record.pid} is a live foreign owner for "
                f"{name!r}; incarnation row {row.get('id')} is active"
            ),
            pid=record.pid,
            expired=expired,
        )
    log.warning(
        "channel inbox dispatcher PID %s is not owned by %s "
        "(no incarnation row%s); not signalling",
        record.pid,
        name,
        "; pidfile expired" if expired else "",
    )
    pid_path.unlink(missing_ok=True)
    return DispatcherStopOutcome(
        stopped=False,
        code=STOP_CODE_REFUSED_UNVERIFIED,
        detail=(
            f"dispatcher PID {record.pid} ownership unverifiable for "
            f"{name!r}: no incarnation row; not signalling"
        ),
        pid=record.pid,
        expired=expired,
    )


__all__ = [
    "PIDFILE_TTL_S",
    "PIDFILE_SCHEMA_VERSION",
    "STOP_CODE_ABSENT",
    "STOP_CODE_REAPED",
    "STOP_CODE_REFUSED_FOREIGN",
    "STOP_CODE_REFUSED_UNVERIFIED",
    "STOP_CODE_STOPPED",
    "STOP_CODE_SUPERSEDED",
    "DispatcherPidRecord",
    "DispatcherStopOutcome",
    "canonical_config_path",
    "close_incarnation_row",
    "pid_alive",
    "pidfile_expired",
    "read_dispatcher_pidfile",
    "read_incarnation_row",
    "registry_known_config_paths",
    "stop_inbox_dispatcher_detailed",
    "touch_dispatcher_pidfile",
    "write_dispatcher_pidfile",
]
