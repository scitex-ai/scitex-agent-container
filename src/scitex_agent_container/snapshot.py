"""Self-snapshot subcommand (todo#286).

Collects a per-agent snapshot of the local host state (tmux/screen,
proc counts, load, memory, fork-pressure, claude context-percent) and
persists it to the container cache dir. On each run, the previous
snapshot is rolled to ``<agent>.prev.json`` and the new one lands in
``<agent>.latest.json`` atomically. A flat dotted-key diff is computed
against the previous snapshot so the dashboard can highlight what
changed.

Kept deliberately stdlib-only: no psutil, no yaml, no new deps.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .context_manager import fetch_agent_meta, get_sensor

# Keys from agent_meta.py we surface in snapshots / status --json.
# `pane_tail` and `pane_tail_block` carry the last N lines of the agent's
# tmux pane (todo#269 / todo#270): consumers (mamba-healer-*, the Agents
# dashboard card #311, fleet_watch.sh diff_one) use them as the cheapest
# liveness signal and to render a live preview of what the agent is doing.
# `recent_actions` is an array of {ts, preview} tool-use snippets from the
# session jsonl, useful for identifying stuck-vs-busy states without a full
# pane capture.
_AGENT_META_KEYS = (
    "alive",
    "subagents",
    "context_pct",
    "current_tool",
    "last_activity",
    "model",
    "pane_tail",
    "pane_tail_block",
    "recent_actions",
)


def _project_agent_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not meta:
        return None
    return {k: meta.get(k) for k in _AGENT_META_KEYS if k in meta}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sidecar thread registry
#
# Phase 2 context_manager kept a dict of live ContextManager instances. We
# expose a small wrapper here so ``snapshot`` can report sidecar liveness
# without reaching into private module state. Process-kind sidecars
# (health_monitor runs as a real thread too in the current codebase, but we
# still model them with ``kind="thread"``) register here as well.
# ---------------------------------------------------------------------------

SidecarInfo = dict[str, Any]
_SIDECARS: dict[str, dict[str, SidecarInfo]] = {}


def register_sidecar(
    agent: str,
    kind: str,
    name: str,
    *,
    pid: int | None = None,
    thread: threading.Thread | None = None,
) -> None:
    """Register a sidecar so ``snapshot`` can introspect liveness.

    ``kind`` is ``"thread"`` or ``"process"``. ``thread`` must be supplied
    for thread-kind sidecars; ``pid`` for process-kind.
    """
    _SIDECARS.setdefault(agent, {})[name] = {
        "kind": kind,
        "pid": pid,
        "thread": thread,
    }


def _sidecar_alive(info: SidecarInfo) -> bool:
    kind = info.get("kind")
    if kind == "thread":
        th = info.get("thread")
        return bool(th is not None and th.is_alive())
    if kind == "process":
        pid = info.get("pid")
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:  # stx-allow: fallback (reason: process probe expected failure)
            return False
        except PermissionError:  # stx-allow: fallback (reason: process probe expected failure)
            # Exists but we can't signal — still alive.
            return True
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            return False
        return True
    return False


def _sidecars_payload(agent: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, info in _SIDECARS.get(agent, {}).items():
        out[name] = {
            "pid": info.get("pid"),
            "kind": info.get("kind"),
            "alive": _sidecar_alive(info),
        }
    return out


# ---------------------------------------------------------------------------
# Cache dir
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    override = os.environ.get("SCITEX_AGENT_CACHE_DIR")
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".scitex" / "agent-container" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _latest_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.latest.json"


def _prev_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.prev.json"


def _diff_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.diff.json"


def _lock_path(agent: str) -> Path:
    return cache_dir() / f"{agent}.lock"


@contextlib.contextmanager
def _snapshot_lock(agent: str) -> Iterator[None]:
    """Per-agent advisory lock around the latest->prev roll + write.

    POSIX fcntl advisory lock; not supported on Windows but container
    targets unix. The lock file persists between calls (reusable); the
    advisory lock is released when the fd is closed via the ``with`` block.
    """
    lock_p = _lock_path(agent)
    with open(lock_p, "w") as fd:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            # Released implicitly on close(), but be explicit for clarity.
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Probes (stdlib-only, Darwin + Linux)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
        return ""


def _probe_tmux() -> tuple[int | None, list[str]]:
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
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
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
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
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
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
        except (OSError, ValueError):  # stx-allow: fallback (reason: file system operation failure)
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
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
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
    except (FileNotFoundError, subprocess.SubprocessError):  # stx-allow: fallback (reason: file may not exist on first use)
        pass
    return {"server": server, "pane": pane}


# ---------------------------------------------------------------------------
# Snapshot assembly + diff
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

    sensor = get_sensor(agent)
    context_percent = sensor.last_percent if sensor is not None else None

    # Prefer a live sensor's cached meta to avoid an extra shell-out per
    # tick. Fall back to a direct fetch when no sensor is running.
    meta_full: dict[str, Any] | None = None
    if sensor is not None and sensor.last_meta is not None:
        meta_full = sensor.last_meta
    else:
        meta_full = fetch_agent_meta(agent)
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


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(obj, list):
        # Lists compare as whole values — don't explode indices.
        out[prefix] = obj
    else:
        out[prefix] = obj
    return out


def compute_diff_fields(prev: dict[str, Any] | None, latest: dict[str, Any]) -> list[str]:
    if prev is None:
        return []
    flat_prev = _flatten(prev)
    flat_latest = _flatten(latest)
    # Ignore timestamp — it always changes.
    ignored = {"timestamp"}
    changed: list[str] = []
    keys = set(flat_prev) | set(flat_latest)
    for k in sorted(keys):
        if k in ignored:
            continue
        if flat_prev.get(k) != flat_latest.get(k):
            changed.append(k)
    return changed


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def take_snapshot(agent: str, *, session: str | None = None, with_diff: bool = True) -> dict[str, Any]:
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
            except (OSError, json.JSONDecodeError):  # stx-allow: fallback (reason: malformed JSON tolerated)
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
            except OSError:  # stx-allow: fallback (reason: file system operation failure)
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
    except (OSError, json.JSONDecodeError):  # stx-allow: fallback (reason: malformed JSON tolerated)
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
    try:
        snap = take_snapshot(agent, session=session)
    except Exception:  # pragma: no cover — defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.exception("snapshot[%s]: tick failed", agent)
        return
    if agent_config is not None and snap.get("has_diff"):
        try:
            from .hooks import run_hook

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
