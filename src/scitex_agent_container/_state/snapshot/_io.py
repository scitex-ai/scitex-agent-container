"""Snapshot gather/take/read public surface + stdlib-only probes.

Collects a per-agent snapshot of the local host state (tmux/screen,
proc counts, load, memory, fork-pressure, claude context-percent) and
persists it to the container cache dir. On each run, the previous
snapshot is rolled to ``<agent>.prev.json`` and the new one lands in
``<agent>.latest.json`` atomically.

Kept deliberately stdlib-only: no psutil, no yaml, no new deps.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._diff import compute_diff_fields
from ._lock import _snapshot_lock
from ._paths import _diff_path, _latest_path, _prev_path
from ._sidecars import _project_agent_meta, _sidecars_payload

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Probes (stdlib-only, Darwin + Linux)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        return ""


def _probe_tmux() -> tuple[int | None, list[str]]:
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        return None, []
    if r.returncode != 0:
        # "no server running" is not an error for us — just zero sessions.
        return 0, []
    names = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return len(names), names


def _probe_screen_count() -> int | None:
    """Return the number of live GNU screen sessions.

    Contract:
    - ``None`` iff the ``screen`` binary is not installed at all.
    - ``0`` if ``screen`` is installed but no sessions are live (``screen -ls``
      prints ``No Sockets found ...`` and exits non-zero — that is NOT an
      error, it means zero sessions).
    - A positive int when one or more sessions are listed.
    """
    if shutil.which("screen") is None:
        return None
    try:
        r = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        # Binary vanished between which() and run(); treat as not installed.
        return None
    combined = (r.stdout or "") + (r.stderr or "")
    if "No Sockets found" in combined:
        return 0
    n = 0
    for ln in (r.stdout or "").splitlines():
        if re.match(r"\s*\d+\.", ln):
            n += 1
    return n


def _probe_claude_pid() -> int | None:
    """Return the PID of the live ``claude`` CLI child, or ``None``.

    The naive ``pgrep -f claude`` matches ANY process whose full command
    line contains the substring ``claude`` — including the
    ``scitex-agent-container`` python wrapper itself (whose argv often
    mentions claude-code, claude_code, or a claude agent name). We must
    exclude that wrapper and only pick the real ``claude`` CLI child that
    ``runtimes/claude_code.py`` execs (command basename == ``claude``).

    Strategy: prefer ``pgrep -n -x claude`` (exact command-name match).
    """
    try:
        r = subprocess.run(
            ["pgrep", "-n", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        return None
    first = (r.stdout or "").strip().splitlines()
    if not first:
        return None
    token = first[0].strip()
    return int(token) if token.isdigit() else None


def _proc_count(pattern: str) -> int | None:
    out = _run(["pgrep", "-af", pattern])
    if not out:
        # Could mean "no matches" or "pgrep missing"; pgrep exit 1 gives
        # empty stdout — treat as zero.
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


def _probe_load1() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return None


def _probe_mem_darwin() -> tuple[int | None, int | None, int | None]:
    out = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    total = int(out.strip()) if out.strip().isdigit() else None
    vm = _run(["vm_stat"])
    if not vm:
        return total, None, None
    page_size = 4096
    m = re.search(r"page size of (\d+) bytes", vm)
    if m:
        page_size = int(m.group(1))
    pages: dict[str, int] = {}
    for ln in vm.splitlines():
        m = re.match(r"(.+?):\s+(\d+)", ln)
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    # Darwin gotcha: "Pages free" alone is always tiny (~100MB) because
    # macOS aggressively uses inactive + speculative pages as cache and
    # reclaims them on demand. Counting only "free" produces false-positive
    # mem-CRITICAL alerts (msg#8603 / todo#310). The true "available"
    # memory is Pages free + Pages inactive + Pages speculative.
    free_pages = (
        pages.get("Pages free", 0)
        + pages.get("Pages inactive", 0)
        + pages.get("Pages speculative", 0)
    )
    free_bytes = free_pages * page_size
    used_bytes = (total - free_bytes) if total is not None else None
    return total, used_bytes, free_bytes


def _probe_mem_linux() -> tuple[int | None, int | None, int | None]:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return None, None, None
    kv: dict[str, int] = {}
    for ln in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", ln)
        if m:
            kv[m.group(1)] = int(m.group(2)) * 1024
    total = kv.get("MemTotal")
    avail = kv.get("MemAvailable", kv.get("MemFree"))
    used = (total - avail) if (total is not None and avail is not None) else None
    return total, used, avail


def _probe_mem() -> tuple[int | None, int | None, int | None]:
    if platform.system() == "Darwin":
        return _probe_mem_darwin()
    if platform.system() == "Linux":
        return _probe_mem_linux()
    return None, None, None


def _probe_nproc() -> tuple[int | None, int | None]:
    """Return (current, max) process counts for fork-pressure math."""
    cur: int | None = None
    mx: int | None = None
    out = _run(["ps", "-A"])
    if out:
        cur = max(0, len(out.splitlines()) - 1)
    if platform.system() == "Darwin":
        mxs = _run(["/usr/sbin/sysctl", "-n", "kern.maxproc"]).strip()
        if mxs.isdigit():
            mx = int(mxs)
    elif platform.system() == "Linux":
        try:
            mx = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
        except (
            OSError,
            ValueError,
        ):  # stx-allow: fallback (reason: file system operation failure)
            mx = None
    return cur, mx


def _probe_tmux_pids(session: str | None) -> dict[str, int | None]:
    server: int | None = None
    pane: int | None = None
    if not session:
        return {"server": None, "pane": None}
    try:
        r = subprocess.run(
            ["tmux", "display", "-p", "-t", f"{session}:0", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            pane = int(r.stdout.strip())
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        pass
    try:
        r = subprocess.run(
            ["pgrep", "-n", "-x", "tmux"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            server = int(r.stdout.strip().splitlines()[0])
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        pass
    return {"server": server, "pane": pane}


# ---------------------------------------------------------------------------
# Snapshot assembly + I/O
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def gather_snapshot(agent: str, *, session: str | None = None) -> dict[str, Any]:
    """Build a snapshot dict for ``agent``. No I/O to cache dir."""
    tmux_count, tmux_names = _probe_tmux()
    screen_count = _probe_screen_count()
    mem_total, mem_used, mem_free = _probe_mem()
    nproc_cur, nproc_max = _probe_nproc()
    if nproc_cur is not None and nproc_max:
        fork_pct: float | None = round(100.0 * nproc_cur / nproc_max, 2)
    else:
        fork_pct = None

    context_percent: float | None = None
    meta_full: dict[str, Any] | None = None
    agent_meta_block = _project_agent_meta(meta_full)
    if (
        context_percent is None
        and agent_meta_block is not None
        and isinstance(agent_meta_block.get("context_pct"), (int, float))
    ):
        context_percent = float(agent_meta_block["context_pct"])

    tmux_pids = _probe_tmux_pids(session or agent)

    claude_pid = _probe_claude_pid()

    return {
        "agent": agent,
        "timestamp": _now_iso(),
        "host": socket.gethostname(),
        "tmux_count": tmux_count,
        "tmux_names": tmux_names,
        "screen_count": screen_count,
        "claude_procs": _proc_count("claude"),
        "bun_procs": _proc_count("bun"),
        "node_procs": _proc_count("node"),
        "load1": _probe_load1(),
        "mem_total_bytes": mem_total,
        "mem_used_bytes": mem_used,
        "mem_free_bytes": mem_free,
        "nproc_cur": nproc_cur,
        "nproc_max": nproc_max,
        "fork_pressure_pct": fork_pct,
        "context_percent": context_percent,
        "agent_meta": agent_meta_block,
        "pids": {
            "container_daemon": os.getpid(),
            "claude_code": claude_pid,
            "tmux": tmux_pids,
            "sidecars": _sidecars_payload(agent),
        },
    }


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def take_snapshot(
    agent: str, *, session: str | None = None, with_diff: bool = True
) -> dict[str, Any]:
    """Gather, persist, and return a snapshot for ``agent``."""
    latest_p = _latest_path(agent)
    prev_p = _prev_path(agent)

    snap = gather_snapshot(agent, session=session)

    with _snapshot_lock(agent):
        # Read previous (before rolling).
        prev_data: dict[str, Any] | None = None
        if latest_p.exists():
            try:
                prev_data = json.loads(latest_p.read_text())
            except (
                OSError,
                json.JSONDecodeError,
            ):  # stx-allow: fallback (reason: malformed JSON tolerated)
                prev_data = None

        if with_diff:
            diff_fields = compute_diff_fields(prev_data, snap)
        else:
            diff_fields = []
        snap["has_diff"] = bool(diff_fields)
        snap["diff_fields"] = diff_fields

        # Roll latest -> prev BEFORE overwriting latest.
        if latest_p.exists():
            try:
                os.replace(latest_p, prev_p)
            except (
                OSError
            ):  # stx-allow: fallback (reason: file system operation failure)
                logger.exception("snapshot[%s]: failed rolling latest to prev", agent)

        _atomic_write_json(latest_p, snap)

        if snap["has_diff"]:
            _atomic_write_json(
                _diff_path(agent),
                {
                    "agent": agent,
                    "timestamp": snap["timestamp"],
                    "diff_fields": diff_fields,
                },
            )

    return snap


def read_latest(agent: str) -> dict[str, Any] | None:
    p = _latest_path(agent)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (
        OSError,
        json.JSONDecodeError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return None


def snapshot_tick(
    agent: str,
    *,
    session: str | None = None,
    agent_config: Any = None,
) -> None:
    """Daemon helper: take a snapshot, swallow errors.

    When ``agent_config`` is supplied and the fresh snapshot has
    ``has_diff``, the configured ``hooks.on_diff`` commands are fired
    via the non-blocking hook pool (todo#286 Phase 4).
    """
    # stx-allow: fallback (reason: daemon tick must not crash the loop; snapshot failures are logged and skipped)
    try:
        snap = take_snapshot(agent, session=session)
    except Exception:  # pragma: no cover — defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.exception("snapshot[%s]: tick failed", agent)
        return
    if agent_config is not None and snap.get("has_diff"):
        # stx-allow: fallback (reason: hook dispatch is best-effort; failure must not disrupt the snapshot cycle)
        try:
            from ...hooks import run_hook

            commands = (getattr(agent_config, "hooks", {}) or {}).get(
                "on_diff", []
            ) or []
            run_hook(
                agent,
                "on_diff",
                commands,
                context={
                    "diff_fields": snap.get("diff_fields", []),
                    "timestamp": snap.get("timestamp"),
                },
            )
        except Exception:  # pragma: no cover — defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            logger.exception("snapshot[%s]: on_diff hook failed", agent)
