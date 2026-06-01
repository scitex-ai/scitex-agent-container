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

# Whitelist used by ``scitex-agent-container show-status --json --terse``.
# Superset of what ``scripts/fleet-watch/probe_remote.sh`` extracts via
# ``jq -r``. Do not remove entries without coordinating with head-nas.
#
# The list below is split into two tranches:
#
#   1. The original 13 fields (todo#300) — the fleet_watch.sh
#      probe_remote.sh whitelist.
#   2. The todo#300 follow-up extension — high-value heartbeat fields
#      promoted from ``collect_rich`` so the MCP sidecar's heartbeat
#      path (PR #66 pivot) can carry them without pulling the ~28 KB
#      full payload. PII / bulky fields are deliberately excluded
#      (``pane_text``, ``claude_md``, ``mcp_json``, ``last_user_msg``,
#      ``stuck_prompt_text``, ``recent_prompts``, ``current_tool_input``,
#      ``recent_tools``) — those remain available in full mode only.
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
    # --- todo#300 follow-up: heartbeat-grade activity/quota fields ----
    # subagent load (both the canonical name and the legacy alias so
    # consumers of either spelling keep working)
    "subagent_count",
    "subagents",
    # context-window usage derived from the transcript (complements the
    # config-side ``context_management.percent`` above)
    "context_pct",
    # Anthropic quota usage + reset timestamps
    "quota_5h_used_pct",
    "quota_7d_used_pct",
    "quota_5h_reset_at",
    "quota_7d_reset_at",
    # classified pane state + last recorded PaneAction
    "pane_state",
    "last_action_at",
    "last_action_name",
    "last_action_outcome",
    # hook-captured tool liveness (LLM-level heartbeat)
    "last_tool_at",
    "last_tool_name",
    # stuck-subagent detection (orochi#133): Agent pretool events with
    # no matching posttool in the ring-buffer window. Non-zero count +
    # non-zero subagent_count is the healer's trigger threshold.
    "open_agent_calls_count",
    "oldest_open_agent_age_s",
    # high-level "what is this agent doing" + tool name only
    # (``current_tool_input`` is intentionally excluded — may carry
    # prompt / path fragments that count as PII)
    "current_task",
    "current_tool",
    # identity / machine affinity
    "account_email",
    "skills_loaded",
    "hostname_canonical",
    "machine",
    # Lead task 2026-06-01: per-agent CPU% + RSS so the terse status
    # surface can attribute host load to individual agents. Absent when
    # the recorded PID is unknown / dead — see
    # ``_state._meta.resources.collect_agent_resources``.
    "cpu_percent",
    "mem_rss_mb",
)

# Whitelist used by ``scitex-agent-container take-snapshot --json --terse``.
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
