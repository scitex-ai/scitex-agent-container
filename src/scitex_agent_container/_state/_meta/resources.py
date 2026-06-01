"""Process / host-resource helpers for ``agent_meta.collect_rich``
and ``sac agents list``.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports ``_pids_from_session`` so
existing test access (``agent_meta._pids_from_session``) keeps working.

Three helpers, three callers:

* ``_pids_from_session`` — resolves a tmux session to (pid, ppid).
  Used by ``agent_meta.collect_rich`` for the rich-status surface.
* ``_collect_host_metrics`` — host-wide CPU/mem/disk via psutil.
  Used by ``agent_meta.collect_rich`` to populate the ``machine``
  block (the dashboard dedupes by hostname).
* ``collect_agent_resources`` — PER-AGENT CPU% + RSS for the
  ``sac agents list`` row, walking the recorded PID + its descendants
  in a single psutil sweep. Lead task 2026-06-01: attribute host load
  to specific agents. Best-effort: dead PIDs return ``None``, never
  raise.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Iterable


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


# ---------------------------------------------------------------------------
# Per-agent process-tree resource probe (sac agents list, lead task 2026-06-01)
# ---------------------------------------------------------------------------


# When the registry doesn't know an agent's PID, ``_pids_from_session``
# returns ``0`` as the sentinel. ``psutil.Process(0)`` resolves to the
# kernel idle process on Linux which would surface bogus aggregate data
# (or crash on AccessDenied). Treat 0 as "no PID known" everywhere.
_UNKNOWN_PID_SENTINEL = 0

# How long to sleep between the prime call and the readout call for
# ``cpu_percent``. Process-level ``cpu_percent`` needs two samples to
# compute a delta. We do ONE batched sleep for the whole sweep instead
# of one per process so the wall-clock cost is constant in agent count.
_CPU_SAMPLE_INTERVAL_S = 0.1


def collect_agent_resources(
    pids: Iterable[int],
    *,
    cpu_sample_interval_s: float = _CPU_SAMPLE_INTERVAL_S,
) -> dict[int, dict[str, float] | None]:
    """Return per-PID ``{"cpu_percent": float, "mem_rss_mb": float}`` or
    ``None`` when the PID is dead/unknown/unprobable.

    Each input PID is treated as the root of an agent's container
    process tree; CPU% and RSS are summed over the root + its
    descendants in a single psutil sweep. CPU% is computed across a
    single batched ``cpu_sample_interval_s`` sleep, NOT per process,
    so the wall-clock cost is ~``cpu_sample_interval_s`` regardless
    of how many agents are registered.

    Contract:

    * The output dict has exactly the same keyspace as ``pids`` (a
      dict, not a list — caller looks up by recorded PID).
    * A PID whose process does not exist, was never created, or is
      ``_UNKNOWN_PID_SENTINEL`` (``0`` — the registry's "unknown"
      placeholder) maps to ``None``. Never to a zero-filled dict —
      the observability skill is explicit that ``absent ≠ 0`` so
      consumers can distinguish "not probed" from "empty".
    * Partial process-tree death (root alive, one descendant just
      exited) does NOT void the whole result — the alive members are
      still summed in. The probe is best-effort by design.
    * Empty input → empty output (no sleep, no psutil call).
    * Hard psutil unavailability (import error) → every key maps to
      ``None`` (NOT an exception). The list command must still render.

    Args:
        pids: The registered PID of each agent (the apptainer
            container root; descendants are discovered).
        cpu_sample_interval_s: Sleep between prime and readout. Defaults
            to 100ms — long enough for cpu_percent to compute a non-
            zero delta on a busy core, short enough to not visibly
            slow ``sac agents list``.

    Returns:
        ``dict[int, dict[str, float] | None]`` keyed by input PID.
    """
    pid_list = list(pids)
    if not pid_list:
        return {}

    # stx-allow: fallback (reason: psutil is an optional dependency;
    # absent on minimal installs — every agent absent-outs, list still
    # renders without resource columns)
    try:
        import psutil as _psutil
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {pid: None for pid in pid_list}

    # 1. Build process trees in one pass. Bad PIDs short-circuit to
    #    ``None`` here so we never call ``cpu_percent`` on them.
    trees: dict[int, list[Any] | None] = {}
    for pid in pid_list:
        if pid == _UNKNOWN_PID_SENTINEL or pid < 0:
            trees[pid] = None
            continue
        # stx-allow: fallback (reason: NoSuchProcess / AccessDenied /
        # ZombieProcess all map to "not probable" — None propagates to
        # the caller, who renders ``-`` in the table)
        try:
            root = _psutil.Process(pid)
            descendants = root.children(recursive=True)
            trees[pid] = [root, *descendants]
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            trees[pid] = None

    # 2. Prime cpu_percent counters. The first call always returns 0.0
    #    (or a meaningless reading); the second call after a sleep
    #    returns the delta. Errors here are non-fatal — they just mean
    #    that process's contribution falls out of the sum.
    for tree in trees.values():
        if tree is None:
            continue
        for proc in tree:
            # stx-allow: fallback (reason: process may die between
            # children() and cpu_percent(); we just drop it from the sum)
            try:
                proc.cpu_percent(interval=None)
            except Exception:  # stx-allow: fallback (reason: see inline comment)
                pass

    # 3. ONE batched sleep for the whole sweep.
    time.sleep(cpu_sample_interval_s)

    # 4. Readout: cpu_percent delta + rss snapshot, summed per tree.
    out: dict[int, dict[str, float] | None] = {}
    for pid, tree in trees.items():
        if tree is None:
            out[pid] = None
            continue
        cpu_sum = 0.0
        rss_sum = 0
        contributing = 0
        for proc in tree:
            # stx-allow: fallback (reason: per-process probe can fail
            # mid-readout; we just skip that contribution and keep
            # summing the rest — partial is better than absent)
            try:
                cpu_sum += float(proc.cpu_percent(interval=None))
                rss_sum += int(proc.memory_info().rss)
                contributing += 1
            except Exception:  # stx-allow: fallback (reason: see inline comment)
                continue
        if contributing == 0:
            # The root and every descendant died between step 1 and 4.
            # Treat as "not probable" rather than reporting zero.
            out[pid] = None
            continue
        out[pid] = {
            "cpu_percent": round(cpu_sum, 1),
            "mem_rss_mb": round(rss_sum / _MB, 1),
        }
    return out
