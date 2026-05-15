"""Process / host-resource helpers for ``agent_meta.collect_rich``.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports ``_pids_from_session`` so
existing test access (``agent_meta._pids_from_session``) keeps working.

``_collect_host_metrics`` is the inline psutil block from ``collect_rich``
lifted verbatim — moved so the parent module shrinks below the ceiling.
"""

from __future__ import annotations

import subprocess
from typing import Any


def _pids_from_session(session: str, multiplexer: str) -> tuple[int, int]:
    pid = 0
    ppid = 0
    if multiplexer != "tmux":
        return pid, ppid
    # stx-allow: fallback (reason: tmux session may not exist yet or pgrep
    # may return no results — pid/ppid of 0 is a valid "unknown" sentinel)
    try:
        out = (
            subprocess.run(
                ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .splitlines()
        )
        if out:
            ppid = int(out[0])
            ps = (
                subprocess.run(
                    ["pgrep", "-P", str(ppid), "-f", "claude"],
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .splitlines()
            )
            pid = int(ps[0]) if ps else ppid
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass
    return pid, ppid


_MB = 1024 * 1024


def _collect_host_metrics() -> dict[str, Any]:
    """Return psutil-derived CPU/mem/disk metrics. ``{}`` on any failure.

    Host-level (not agent-level) — the hub dedupes under ``machine``.
    """
    # stx-allow: fallback (reason: psutil is an optional dependency; absent
    # on minimal installs — metrics dict stays empty, dashboard handles it)
    try:
        import psutil as _psutil

        _cpu_pct = _psutil.cpu_percent(interval=None)
        _vm = _psutil.virtual_memory()
        _disk = _psutil.disk_usage("/")
        _load = _psutil.getloadavg()
        _cpu_count = _psutil.cpu_count(logical=True) or 0
        # stx-allow: fallback (reason: cpu_freq may be None on VMs/containers)
        try:
            _freq = _psutil.cpu_freq()
            _cpu_model = f"{_cpu_count}x @ {_freq.max:.0f}MHz" if _freq else ""
        except Exception:
            _cpu_model = ""
        return {
            "cpu_count": _cpu_count,
            "cpu_model": _cpu_model,
            "cpu_used_percent": round(_cpu_pct, 1),
            "load_avg_1m": round(_load[0], 2),
            "load_avg_5m": round(_load[1], 2),
            "load_avg_15m": round(_load[2], 2),
            "mem_used_percent": round(_vm.percent, 1),
            "mem_total_mb": round(_vm.total / _MB, 1),
            "mem_free_mb": round(_vm.available / _MB, 1),
            "mem_used_mb": round((_vm.total - _vm.available) / _MB, 1),
            "disk_used_percent": round(_disk.percent, 1),
            "disk_total_mb": round(_disk.total / _MB, 1),
            "disk_used_mb": round(_disk.used / _MB, 1),
            "resource_source": "local",
        }
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return {}
