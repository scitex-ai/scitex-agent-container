"""Terse projection for ``status --json`` and ``snapshot --json`` (todo#300).

Full status/snapshot payloads grew ~18x after todo#286 field additions.
``fleet_watch.sh`` polls every host every 5 min across ~9 agents and only
reads a small subset of the JSON. ``--terse`` projects the payload to a
whitelist superset of what ``probe_remote.sh`` extracts, cutting per-agent
bytes to the minimum fleet_watch actually needs.

Design:
    * Output is a **flat dict with dotted keys**. Flat form round-trips
      cleanly through ``jq -r '.["foo.bar"]'`` and guarantees a stable
      shape regardless of whether the source nested dict was present.
    * Absent / unknown source fields are emitted as ``null`` (not omitted)
      so consumers never have to branch on key presence.
    * Zero new dependencies — stdlib only.
"""

from __future__ import annotations

from typing import Any, Iterable

# Whitelist used by ``scitex-agent-container status --json --terse``.
# Superset of what ``scripts/fleet-watch/probe_remote.sh`` extracts via
# ``jq -r``. Do not remove entries without coordinating with head-nas.
TERSE_STATUS_FIELDS: tuple[str, ...] = (
    # identity
    "agent",
    "state",
    "timestamp",
    # liveness
    "tmux_alive",
    "last_post_ts",
    # context window
    "context_management.percent",
    "context_management.strategy",
    "context_management.trigger_at_percent",
    # pids for kill -0 liveness
    "pids.claude_code",
    "pids.container_daemon",
    # health summary
    "health.ok",
    # snapshot summary (NOT diff_fields, NOT the full snapshot)
    "snapshot.timestamp",
    "snapshot.has_diff",
)

# Whitelist used by ``scitex-agent-container snapshot --json --terse``.
TERSE_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "agent",
    "timestamp",
    "host",
    "tmux_count",
    "screen_count",
    "claude_procs",
    "bun_procs",
    "load1",
    "mem_total_bytes",
    "mem_used_bytes",
    "mem_free_bytes",
    "fork_pressure_pct",
    "context_percent",
    "has_diff",
    "pids.claude_code",
    "pids.container_daemon",
)


def _get_dotted(source: dict, dotted: str) -> Any:
    """Walk ``dotted`` keys into ``source``; return ``None`` if any hop misses."""
    cur: Any = source
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        if part not in cur:
            return None
        cur = cur[part]
    return cur


def project_terse(source: dict, fields: Iterable[str]) -> dict:
    """Project ``source`` onto ``fields``, emitting ``null`` for missing keys.

    Returns a flat dict keyed by the exact dotted field names in ``fields``.
    Consumers can extract via ``jq -r '.["context_management.percent"]'``.
    """
    out: dict[str, Any] = {}
    for key in fields:
        out[key] = _get_dotted(source, key)
    return out
