"""Stale ``instances`` lease cleanup on the agent start path.

The ``instances`` table is the "one-at-a-time" lease for an agent name
on a host: a row with ``ended_at IS NULL`` means "this agent is
running here". When a container dies WITHOUT going through
:func:`agent_stop` (kernel OOM, host reboot, ``kill -9``, container
runtime crash), the row is never marked ended — the row is **stale**
and points at a PID that no longer exists.

Before this helper, the operator's manual workaround was::

    sqlite3 ~/.scitex/agent-container/state.db \
        "DELETE FROM instances WHERE name='<name>' AND ended_at IS NULL"

…otherwise the next ``sac agents start`` saw the stale row, the
already-running check fired, and the start no-op'd. This helper
automates that DELETE into the start path: when the row exists but
the recorded PID is demonstrably dead, the row is marked ended
(``exit_reason='stale-cleared'``) and the start proceeds normally.

Design notes:

* The helper runs ONLY on the "not really running" branch of
  :func:`._start.agent_start` — i.e. when ``runtime.is_running``
  returns False. A live runtime is the strongest signal that the
  lease is legitimate; we never clear a row that the runtime still
  vouches for.

* Atomicity: each stale row is closed via :func:`record_instance_stop`
  which sets ``ended_at`` + writes a paired ``events`` row in a single
  SQLite transaction — concurrent starts cannot see a half-cleared
  state.

* Per-row PID truth: when ``row['pid']`` is non-null we probe it with
  ``os.kill(pid, 0)``. When ``pid`` is NULL (the common case for
  pre-#XYZ local starts — :func:`record_local_instance` did not pass
  ``pid``), the row is treated as stale ONLY if the runtime separately
  reports the agent dead — that gate is the caller's responsibility
  (see :func:`._start.agent_start`).
"""

from __future__ import annotations

import os
from typing import Callable, Iterable


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live process on this host.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for a dead PID
    and ``PermissionError`` for a live PID owned by another user
    (still proof of life). Any other ``OSError`` → treat as
    indeterminate → "not alive" so the row gets cleared rather than
    pinned forever.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Live process owned by a different uid — still proof of life.
        return True
    except OSError:
        # stx-allow: fallback (reason: indeterminate kernel error —
        # degrade to "not alive" so a stuck lease does not block a
        # legitimate restart)
        return False


def clear_stale_instance_lease(
    name: str,
    *,
    instances_oracle: Callable[[], Iterable[dict]] | None = None,
    stop_writer: Callable[[str, str], bool] | None = None,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
) -> int:
    """Close ``instances`` rows for ``name`` whose recorded PID is dead.

    Returns the number of rows cleared (0 when nothing was stale).

    The two collaborator seams (``instances_oracle`` /
    ``stop_writer``) default to the real state.db helpers. Tests pass
    a real on-disk ``state.db`` via the ``isolated_state_db`` fixture
    so the defaults exercise the real code path — no mocks.

    Cleanup contract for the caller (see :func:`._start.agent_start`):

    * Call this ONLY when the runtime's ``is_running`` reports the
      agent dead. A live row + live runtime must never be cleared.
    * A row whose ``pid`` is NULL is left alone here — without a PID
      we have no per-row proof of deadness. The caller's
      runtime-is-dead precondition is the only justification we'd
      need to clear a NULL-pid row, and that responsibility is kept
      with the caller so this helper stays a pure
      "verify-pid + close" primitive.
    """
    if instances_oracle is None:
        from .._state.state_db import list_active_instances as _list

        def instances_oracle():  # type: ignore[no-redef]
            return _list(host=None)

    if stop_writer is None:
        from .._state.state_db import record_instance_stop as _stop

        def stop_writer(row_id: str, reason: str) -> bool:  # type: ignore[no-redef]
            return _stop(row_id, exit_reason=reason)

    try:
        rows = list(instances_oracle())
    except Exception:
        # stx-allow: fallback (reason: a missing / locked state.db
        # must not block the start path; degrade to "nothing cleared"
        # and let the caller proceed)
        return 0

    cleared = 0
    for row in rows:
        if row.get("name") != name:
            continue
        pid = row.get("pid")
        if pid is None:
            # No per-row proof of deadness; leave the row alone here.
            # The caller's runtime-is-dead precondition would justify
            # a clear, but we keep that policy in the caller so this
            # primitive stays narrow.
            continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        if pid_alive_fn(pid_int):
            continue
        row_id = row.get("id")
        if not row_id:
            continue
        try:
            if stop_writer(str(row_id), "stale-cleared"):
                cleared += 1
        except Exception:
            # stx-allow: fallback (reason: a failed UPDATE must not
            # block the start path; the next start retries)
            continue

    # v4 step 5 — the on-disk ``instance_id`` marker is the same
    # staleness one directory over: a crashed run never reached
    # ``agent_stop``'s ``clear_instance_id``, so its incarnation id is
    # still lying in the state dir. The caller's precondition (runtime
    # reports the agent DEAD) is exactly the justification to remove it,
    # and removing it matters now that the NEXT runner ADOPTS the first
    # fresh marker it sees at boot (bind-once — ``_runners._incarnation``):
    # the boot window must never offer a previous incarnation's id.
    # ``record_local_instance`` rewrites the marker right after a
    # successful launch, so nothing is lost on the happy path.
    try:
        from .._runners._session_state import clear_instance_id, state_dir_for

        clear_instance_id(state_dir_for(name))
    except Exception:
        # stx-allow: fallback (reason: an unreadable runtime dir must not
        # block the start path; the bind-once mtime grace still guards the
        # runner against a stale marker)
        pass
    return cleared


__all__ = ["clear_stale_instance_lease"]
