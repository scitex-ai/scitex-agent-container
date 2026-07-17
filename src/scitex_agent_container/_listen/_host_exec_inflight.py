"""What ``host_exec`` is running right now — registry + ``GET
/v1/host_exec/inflight``.

Split out of :mod:`._host_exec`. Exists because during the 2026-07-17 incident
the only signal a caller had was an EMPTY RESPONSE, which is equally consistent
with a hang, a network fault, a dead listener, and a genuinely empty result.
All four had to be separated by hand, and the first reading was wrong. A
blocked caller should be able to READ what is running, not infer it from
silence.

This registry is NOT a lock and NOT a cap. ``host_exec`` does not serialise:
concurrent callers run concurrently, each on its own dedicated thread. Stated
explicitly because an undocumented global lock is precisely how the starvation
this fix removes stayed invisible.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class InflightExec:
    """One host_exec currently running, for the ``/inflight`` probe."""

    exec_id: int
    caller: str
    argv: tuple[str, ...]
    timeout_s: float
    started_monotonic: float

    def running_s(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)


_inflight_lock = threading.Lock()
_inflight: dict[int, InflightExec] = {}
_exec_ids = itertools.count(1)


def next_exec_id() -> int:
    """Monotonic id for one exec. ``itertools.count`` is atomic under the GIL
    for this use, but the handler treats ids as opaque anyway."""
    return next(_exec_ids)


def inflight_snapshot() -> list[InflightExec]:
    """Currently-running host_execs, oldest first."""
    with _inflight_lock:
        entries = list(_inflight.values())
    return sorted(entries, key=lambda e: e.started_monotonic)


def register_inflight(entry: InflightExec) -> None:
    with _inflight_lock:
        _inflight[entry.exec_id] = entry


def unregister_inflight(exec_id: int) -> None:
    with _inflight_lock:
        _inflight.pop(exec_id, None)


async def host_exec_inflight(request: Request) -> JSONResponse:
    """``GET /v1/host_exec/inflight`` — what host_exec is running right now.

    Deliberately NOT gated by ``_host_exec.ELIGIBLE_GROUPS``: this is the
    diagnostic to reach for when the fleet's host arm looks stuck, so it must
    not require the privileges of the thing that is stuck. It exposes the argv
    of in-flight commands to any bearer-authed caller — the same audience that
    can already read the audit log.
    """
    entries = inflight_snapshot()
    return JSONResponse(
        {
            "running": len(entries),
            "oldest_running_s": entries[0].running_s() if entries else 0.0,
            "execs": [
                {
                    "exec_id": e.exec_id,
                    "caller": e.caller,
                    "argv": list(e.argv),
                    "timeout_s": e.timeout_s,
                    "running_s": e.running_s(),
                }
                for e in entries
            ],
        }
    )


__all__ = [
    "InflightExec",
    "host_exec_inflight",
    "inflight_snapshot",
    "next_exec_id",
    "register_inflight",
    "unregister_inflight",
]
