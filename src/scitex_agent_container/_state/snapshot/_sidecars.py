"""Sidecar thread/process registry and liveness probes.

Phase 2 context_manager kept a dict of live ContextManager instances. We
expose a small wrapper here so ``snapshot`` can report sidecar liveness
without reaching into private module state. Process-kind sidecars
(health_monitor runs as a real thread too in the current codebase, but we
still model them with ``kind="thread"``) register here as well.
"""

from __future__ import annotations

import os
import threading
from typing import Any

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
        except (
            ProcessLookupError
        ):  # stx-allow: fallback (reason: process probe expected failure)
            return False
        except (
            PermissionError
        ):  # stx-allow: fallback (reason: process probe expected failure)
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
