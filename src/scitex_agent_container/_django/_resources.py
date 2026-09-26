"""Process-resource observation for the detail view.

HONEST, NOT FABRICATED. Agents run inside apptainer and their listener-reported
``pid`` usually belongs to ANOTHER pid namespace — from the web process's
namespace, ``/proc/<pid>`` does not exist. Reading it and reporting a number
would be a lie; so we try, and when the pid is not in our namespace we return an
explicit ``namespace`` state instead of inventing RSS/CPU.

When the pid IS readable (e.g. a host-runtime agent, or the dashboard's own
processes), we report real ``/proc`` values: RSS, VSZ, user+sys CPU, and the
direct process-tree size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _proc_status_field(pid: int, field: str) -> str | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _proc_cpu_ticks(pid: int) -> int | None:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (OSError, IndexError, ValueError):
        return None
    # fields 14,15 (0-based 11,12 after comm split) = utime, stime
    try:
        return int(parts[11]) + int(parts[12])
    except (IndexError, ValueError):
        return None


def _children(pid: int) -> list[int]:
    """Immediate children via /proc/<pid>/task/*/children (best-effort)."""
    kids: set[int] = set()
    task = Path(f"/proc/{pid}/task")
    try:
        for t in task.iterdir():
            try:
                raw = (t / "children").read_text(encoding="utf-8")
                kids.update(int(x) for x in raw.split())
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(kids)


def read_resources(pid: Any) -> dict[str, Any]:
    """Return ``{"state": ..., "pid": ..., <fields>}`` for a listener-reported pid.

    ``state`` is one of:
      * ``unavailable`` — no pid reported (agent not started / dead)
      * ``namespace``   — pid present but not in this process's pid namespace
      * ``ok``          — /proc readable; real rss/vsz/cpu/tree reported
    """
    if pid in (None, ""):
        return {"state": "unavailable", "pid": None}
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return {"state": "unavailable", "pid": pid}
    if p <= 0 or not Path(f"/proc/{p}").is_dir():
        # p <= 0 is malformed input (a real pid is positive) -> unavailable.
        # A positive pid with no /proc entry is the honest "namespace" state:
        # the listener saw it, this process's pid namespace does not (apptainer
        # isolation) — never a fabricated number.
        if p <= 0:
            return {"state": "unavailable", "pid": p}
        return {
            "state": "namespace",
            "pid": p,
            "reason": "agent pid is not visible from this process's pid namespace (apptainer isolation)",
        }
    rss = _proc_status_field(p, "VmRSS")  # e.g. "19500 kB"
    vsz = _proc_status_field(p, "VmSize")
    cpu_ticks = _proc_cpu_ticks(p)
    tree = [p] + _children(p)
    return {
        "state": "ok",
        "pid": p,
        "rss": rss,
        "vsz": vsz,
        "cpu_ticks": cpu_ticks,
        "tree_size": len(tree),
        "children": _children(p)[:16],
    }


__all__ = ["read_resources"]
