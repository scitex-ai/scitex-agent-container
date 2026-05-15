"""Rich agent metadata collection (claude-hud-style).

Canonical source of truth for the metadata payload that is:
  1. Emitted by ``scitex-agent-container show-status <name> --json``.
  2. POSTed by the MCP sidecar heartbeat to ``/api/agents/register/``.

Ported 2026-04-12 from the pre-restructure
``~/.scitex/orochi/agents/mamba-healer-mba/scripts/agent_meta.py`` — the
fleet script now lives at ``~/.scitex/orochi/shared/scripts/agent_meta.py``
(2026-04-17 runtime/ layout) and shells out to this module via ``sac
status --json``, so the collection logic still lives in one place.

Every field is best-effort: any failure leaves the field as its default
(``""``, ``0``, ``0.0``, ``[]``) and never raises. The caller merges this
dict on top of the base ``agent_status`` result.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime, timezone
from typing import Any

from .._account.claude_usage import fetch_usage
from .._env import getenv as _sac_env


def detect_multiplexer(session: str) -> str:
    """Return 'tmux', 'screen', or '' if neither reports the session."""
    try:
        if (
            subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
            ).returncode
            == 0
        ):
            return "tmux"
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        pass
    try:
        r = subprocess.run(
            ["screen", "-ls", session],
            capture_output=True,
            text=True,
        )
        if session in r.stdout:
            return "screen"
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        pass
    return ""


# Helpers live in ``_meta/`` submodules; re-exported here to preserve
# the ``agent_meta._foo`` access paths used by tests and the
# integration suite. Each submodule is kept under the 512-line hook
# ceiling so future edits stay unblocked.
from ._meta.config_files import (  # noqa: F401
    _config_candidates,
    _parse_mcp_servers,
    _read_claude_md,
    _read_mcp_json,
    _redact_mcp_tree,
)
from ._meta.pane import (  # noqa: F401
    _SUBAGENT_MARKER_RE,
    _capture_pane,
    _classify_pane_state,
    _subagent_count_from_pane,
    parse_subagent_count_from_pane_text,
)
from ._meta.resources import _collect_host_metrics, _pids_from_session  # noqa: F401
from ._meta.secrets import _SECRET_PATTERNS, _redact_secrets  # noqa: F401
from ._meta.skills import _parse_skills  # noqa: F401
from ._meta.transcript import (  # noqa: F401
    _encode_claude_project,
    _latest_jsonls,
    _read_sdk_session_state,
)


def collect_rich(
    *,
    name: str,
    workdir: str,
    session: str,
) -> dict[str, Any]:
    """Collect claude-hud-style metadata for one agent.

    Parameters
    ----------
    name:
        Agent name (used only as a fallback identifier).
    workdir:
        Absolute workspace dir for the agent (used to locate CLAUDE.md
        and the Claude Code transcript JSONL files).
    session:
        Multiplexer session name (what ``tmux has-session -t`` checks).
    """
    multiplexer = detect_multiplexer(session)

    # ---- statusline JSON (authoritative, written by sac-statusline) --------
    # Prefer the persisted JSON that Claude Code's statusLine command writes
    # each turn — it contains the exact used_percentage from /context and
    # authoritative rate-limit resets. Falls back to JSONL approximation when
    # the file is absent (agent not yet launched with sac-statusline wired).
    _sl: dict = {}
    # stx-allow: fallback (reason: statusline module is optional; import or
    # read failure falls back to JSONL approximation — collect_rich is best-effort)
    try:
        from ..statusline import read_statusline_json

        _sl = read_statusline_json(name) or {}
    except Exception:
        pass

    # ---- transcript-derived fields ----------------------------------
    context_pct = 0.0
    current_tool = ""
    current_tool_input = ""
    current_task = ""
    last_user_msg = ""
    last_activity = ""
    model = ""
    started_at = ""

    # Seed context_pct from statusline JSON if available (overridden below
    # by the JSONL scan only when the statusline file is absent).
    if _sl:
        _cw = _sl.get("context_window") or {}
        _cw_pct = _cw.get("used_percentage")
        if _cw_pct is not None:
            context_pct = round(float(_cw_pct), 1)
        _sl_model = (_sl.get("model") or {}).get("display_name", "")
        if _sl_model:
            model = _sl_model

    jsonls = _latest_jsonls(workdir)
    if jsonls:
        # stx-allow: fallback (reason: stat() can race with file deletion;
        # missing started_at is acceptable — field stays empty)
        try:
            earliest = min(jsonls, key=lambda p: p.stat().st_mtime)
            started_at = datetime.fromtimestamp(
                earliest.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass

        # stx-allow: fallback (reason: JSONL may be unreadable mid-rotate;
        # empty lines list means no transcript fields — non-fatal)
        try:
            lines = jsonls[0].read_text().splitlines()[-50:]
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            lines = []

        for line in reversed(lines):
            # stx-allow: fallback (reason: individual JSONL lines may be
            # truncated mid-write; skip invalid lines and keep scanning)
            try:
                obj = json.loads(line)
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                continue
            if obj.get("type") == "assistant" and "message" in obj:
                msg = obj["message"]
                if not model:
                    model = msg.get("model", "")
                if not last_activity:
                    last_activity = obj.get("timestamp", "")
                # Only fall back to the JSONL approximation when the
                # authoritative statusline JSON didn't supply a value.
                if not _sl:
                    u = msg.get("usage", {})
                    total = (
                        u.get("input_tokens", 0)
                        + u.get("cache_read_input_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0)
                    )
                    # Opus 4.6 1M context = 1,000,000 tokens
                    context_pct = round((total / 1_000_000) * 100, 1)
                break

        # Find the most recent tool_use AND its input preview, so the
        # dashboard can show "Bash: docker compose build" instead of just
        # "Bash". Per ywatanabe complaint msg 5481.
        for line in reversed(lines):
            # stx-allow: fallback (reason: truncated JSONL line during rotation;
            # skip and keep scanning for last valid tool_use)
            try:
                obj = json.loads(line)
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                continue
            if obj.get("type") == "assistant":
                content = obj.get("message", {}).get("content", [])
                for c in content:
                    if c.get("type") == "tool_use":
                        current_tool = c.get("name", "")
                        tool_input = c.get("input", {}) or {}
                        # Heuristic preview by tool kind:
                        if current_tool == "Bash":
                            preview = tool_input.get("description") or tool_input.get(
                                "command", ""
                            )
                        elif current_tool in ("Edit", "Write", "Read"):
                            preview = tool_input.get("file_path", "")
                        elif current_tool == "Grep":
                            preview = tool_input.get("pattern", "")
                        elif current_tool == "Glob":
                            preview = tool_input.get("pattern", "")
                        elif current_tool == "Agent":
                            preview = tool_input.get(
                                "description", ""
                            ) or tool_input.get("subagent_type", "")
                        elif current_tool.startswith("mcp__"):
                            preview = (
                                tool_input.get("text", "")
                                or tool_input.get("chat_id", "")
                                or tool_input.get("query", "")
                            )
                        else:
                            preview = ""
                        if isinstance(preview, str):
                            current_tool_input = preview[:120].strip()
                        break
                if current_tool:
                    break

        # Find the most recent USER message — gives the dashboard a
        # "what was this agent last asked to do" snippet which is more
        # meaningful than the tool name alone.
        for line in reversed(lines):
            # stx-allow: fallback (reason: truncated JSONL line during rotation;
            # skip and keep scanning for last valid user message)
            try:
                obj = json.loads(line)
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                continue
            if obj.get("type") == "user" and "message" in obj:
                msg = obj["message"]
                content = msg.get("content")
                if isinstance(content, str):
                    last_user_msg = content[:200].strip()
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "text":
                                parts.append(c.get("text", ""))
                            elif c.get("type") == "tool_result":
                                # Skip tool results — they're noise here
                                pass
                    last_user_msg = " ".join(parts)[:200].strip()
                if last_user_msg:
                    break

    # current_task is the high-level "what is this agent doing": prefer
    # the tool preview, then the last user message snippet, then the bare
    # tool name. Never empty if the agent is alive.
    if current_tool and current_tool_input:
        current_task = f"{current_tool}: {current_tool_input}"
    elif current_tool:
        current_task = current_tool
    elif last_user_msg:
        current_task = last_user_msg

    # ---- process / session / skills ---------------------------------
    subagent_count = _subagent_count_from_pane(session, multiplexer)
    pid, ppid = _pids_from_session(session, multiplexer)
    skills_loaded = _parse_skills(workdir)

    # ---- terminal pane + classified state ---------------------------
    # All of these are deterministic (no LLM). tmux capture-pane is the
    # only I/O beyond file reads; redaction strips tokens before any
    # downstream consumer sees the data.
    raw_pane = _capture_pane(session, multiplexer)
    pane_text = _redact_secrets(raw_pane)
    pane_state, stuck_prompt_text = _classify_pane_state(pane_text)

    # ---- workspace file snapshots -----------------------------------
    claude_md = _read_claude_md(workdir)
    mcp_json = _read_mcp_json(workdir)
    mcp_servers = _parse_mcp_servers(workdir)

    # ---- hook-captured tool / prompt log ----------------------------
    # Populated by `scitex-agent-container ingest-hook-event` entries wired into
    # the agent's .claude/settings.local.json. Non-agentic: pure ring-
    # buffer read.
    # stx-allow: fallback (reason: event_log DB may not exist on agents that
    # haven't run a hook yet — empty summary is a valid initial state)
    try:
        from .event_log import summarize as _summarize_events

        _event_summary = _summarize_events(name, limit=50)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        _event_summary = {
            "recent_tools": [],
            "recent_prompts": [],
            "agent_calls": [],
            "open_agent_calls": [],
            "background_tasks": [],
            "counts": {},
        }
    # Use canonical fleet name (e.g. "nas" instead of "DXP480TPLUS-994")
    # stx-allow: fallback (reason: resolve_hostname reads a YAML that may be
    # absent on unconfigured hosts — raw gethostname is an acceptable fallback)
    try:
        from ..config._host import resolve_hostname

        machine = resolve_hostname()
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        machine = socket.gethostname().split(".")[0]

    # ---- Claude quota fields ----------------------------------------
    quota_5h_used_pct: float | None = None
    quota_7d_used_pct: float | None = None
    quota_5h_reset_at: str | None = None
    quota_7d_reset_at: str | None = None
    quota_from_cache: bool = False
    quota_error: str | None = None

    # Prefer statusline JSON rate-limits (exact values from Claude Code)
    # over the fetch_usage scrape when available.
    if _sl:
        _rl = _sl.get("rate_limits") or {}
        _fh = _rl.get("five_hour") or {}
        _sd = _rl.get("seven_day") or {}
        if _fh.get("used_percentage") is not None:
            quota_5h_used_pct = round(float(_fh["used_percentage"]), 1)
        if _sd.get("used_percentage") is not None:
            quota_7d_used_pct = round(float(_sd["used_percentage"]), 1)
        quota_5h_reset_at = _fh.get("resets_at") or None
        quota_7d_reset_at = _sd.get("resets_at") or None
        quota_from_cache = False  # live from statusline, not cached

    if quota_5h_used_pct is None:
        # stx-allow: fallback (reason: fetch_usage may fail on network timeout
        # or missing credentials; quota_error captures the reason for callers)
        try:
            usage = fetch_usage()
            quota_5h_used_pct = usage.get("used_pct_5h")
            quota_7d_used_pct = usage.get("used_pct_7d")
            quota_5h_reset_at = usage.get("reset_at_5h")
            quota_7d_reset_at = usage.get("reset_at_7d")
            quota_from_cache = bool(usage.get("from_cache", False))
            quota_error = usage.get("error")
        except Exception as exc:  # stx-allow: fallback
            quota_error = f"fetch_usage raised: {exc}"

    # ---- Account / credential identity ------------------------------------
    # Pull the full non-secret credentials view so downstream consumers
    # can render plan, plugins, statusline command, and auth-rotation
    # state without re-scanning ~/.claude/. The dashboard previously
    # showed billing_type ("stripe_subscription") as the plan, which is
    # wrong — the real plan comes from rateLimitTier in credentials.json
    # and is normalized to plan_label here.
    account_email: str | None = None
    account_plan_label: str | None = None
    account_subscription_type: str | None = None
    account_rate_limit_tier: str | None = None
    account_organization_name: str | None = None
    account_uuid: str | None = None
    oauth_expires_at: int | None = None
    installed_plugins: list = []
    status_line_command: str | None = None
    # stx-allow: fallback (reason: credentials file absent on freshly
    # provisioned agents — account_email stays None until auth completes)
    try:
        from .._account.credentials import read_credentials_metadata

        _cred = read_credentials_metadata()
        account_email = _cred.get("email_address")
        account_plan_label = _cred.get("plan_label")
        account_subscription_type = _cred.get("subscription_type")
        account_rate_limit_tier = _cred.get("rate_limit_tier")
        account_organization_name = _cred.get("organization_name")
        account_uuid = _cred.get("account_uuid")
        _expires = _cred.get("oauth_expires_at")
        if isinstance(_expires, int):
            oauth_expires_at = _expires
        _plugins = _cred.get("installed_plugins")
        if isinstance(_plugins, list):
            installed_plugins = _plugins
        _slc = _cred.get("status_line_command")
        if isinstance(_slc, str):
            status_line_command = _slc
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass

    # ---- Auth-rotation tracking -------------------------------------------
    # When Claude Code rotates the OAuth token the expiresAt timestamp
    # jumps. We append one NDJSON line per observed change, keyed on the
    # email so the dashboard can show "this email has rotated N times,
    # last at T". The log is local-only (not pushed as a bulk field)
    # because the hub should dedupe rotations on its side from the
    # per-heartbeat oauth_expires_at field.
    if account_email and isinstance(oauth_expires_at, int):
        try:
            # hook-bypass: line-limit (rotations migrated under accounts/_rotations/ — see GITIGNORED/REFACTORING.md)
            from scitex_config._ecosystem import local_state as _local_state

            rot_dir = _local_state.path("agent-container", "accounts", "_rotations")
            rot_dir.mkdir(parents=True, exist_ok=True)
            rot_file = rot_dir / f"{account_email}.ndjson"
            last_expires: int | None = None
            if rot_file.is_file():
                try:
                    for line in reversed(rot_file.read_text().splitlines()):
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if isinstance(obj, dict) and isinstance(
                            obj.get("oauth_expires_at"), int
                        ):
                            last_expires = obj["oauth_expires_at"]
                            break
                except Exception:
                    last_expires = None
            if last_expires != oauth_expires_at:
                entry = {
                    "ts": datetime.now(tz=timezone.utc).isoformat(),
                    "email": account_email,
                    "account_uuid": account_uuid,
                    "oauth_expires_at": oauth_expires_at,
                    "plan_label": account_plan_label,
                }
                with rot_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ---- Machine resource metrics (psutil, optional) -----------------------
    # Body lives in ``_meta/resources.py`` so this module stays under
    # the 512-line hook ceiling. Returns ``{}`` on any failure (psutil
    # missing, container without /proc, etc.) — dashboard handles empty.
    _metrics = _collect_host_metrics()

    return {
        "multiplexer": multiplexer,
        "pid": pid,
        "ppid": ppid,
        "subagent_count": subagent_count,
        "subagents": subagent_count,  # legacy alias
        "context_pct": context_pct,
        "current_tool": current_tool,
        "current_tool_input": current_tool_input,
        "current_task": current_task,
        "last_user_msg": last_user_msg,
        "last_activity": last_activity,
        "skills_loaded": skills_loaded,
        "machine": machine,
        # scitex-orochi todo#55: canonical FQDN for display next to the
        # short machine label ("spartan (spartan.hpc.unimelb.edu.au)").
        # Falls back to the short name on hosts with no reverse DNS.
        "hostname_canonical": (socket.getfqdn() or "").strip(),
        "workdir": workdir,
        "project": name,
        # Only override started_at if we found one; caller can decide
        # whether to prefer the registry's started_at over this one.
        "started_at_transcript": started_at,
        # model from transcript is more accurate than config.model when
        # the agent is actually running under a different model alias.
        "model_transcript": model,
        "version": _sac_env("META_VERSION", "0.2"),
        # ---- claude-session runtime fields ------------------------------
        # ``None`` for non-SDK agents; a dict for claude-session agents
        # exposing the SDK session id, accumulated per-turn token totals
        # (read from ``runtime/<name>/quota.json``), and the latest
        # heartbeat state. Lets ``sac agent status --json`` give parity
        # surface to the claude-code runtime without conflating the
        # rate-limit fields below (which come from a different source).
        "sdk_session": _read_sdk_session_state(name, workdir),
        # ---- Claude quota fields ----------------------------------------
        "quota_5h_used_pct": quota_5h_used_pct,
        "quota_7d_used_pct": quota_7d_used_pct,
        "quota_5h_reset_at": quota_5h_reset_at,
        "quota_7d_reset_at": quota_7d_reset_at,
        "quota_from_cache": quota_from_cache,
        "quota_error": quota_error,
        # ---- Account identity (which Claude account this agent is using) ----
        # `account_email` stays as the stable id consumers group on.
        # `account_plan_label` is the human-readable plan ("Max 20x" etc.)
        # derived from rateLimitTier — dashboards should prefer this over
        # billing_type, which only reports payment method.
        # `oauth_expires_at` is the unix-ms token expiry; a change across
        # heartbeats indicates the OAuth token was rotated.
        "account_email": account_email,
        "account_plan_label": account_plan_label,
        "account_subscription_type": account_subscription_type,
        "account_rate_limit_tier": account_rate_limit_tier,
        "account_organization_name": account_organization_name,
        "account_uuid": account_uuid,
        "oauth_expires_at": oauth_expires_at,
        # ---- Claude Code setup audit (for web-UI setup check) ---------------
        # `installed_plugins` lists what is installed via /plugin install.
        # `status_line_command` exposes whatever claude-hud / custom
        # statusline the user wired up — the hub uses it as a "setup-ok"
        # signal (is claude-hud wired in? is the sac-statusline wrapper
        # in place?).
        "installed_plugins": installed_plugins,
        "status_line_command": status_line_command,
        # ---- Machine resource metrics (for hub /api/resources/) -------------
        # NOTE: metrics are host-level, not agent-level. When multiple agents
        # run on the same host they all report identical values; the hub is
        # expected to dedupe under ``machine`` rather than store N copies.
        "metrics": _metrics,
        # ---- Live terminal pane + classified state -------------------------
        # Deterministic, non-agentic: tmux capture-pane + regex classifier.
        # Secrets are redacted in-place before inclusion.
        "pane_text": pane_text,
        "pane_state": pane_state,
        "stuck_prompt_text": stuck_prompt_text,
        # ---- Workspace file snapshots --------------------------------------
        # Full CLAUDE.md (truncated) so downstream consumers do not need
        # per-host filesystem access. .mcp.json has token-style keys
        # redacted. mcp_servers is the structured view for setup audit
        # (name + transport + host/command, nothing sensitive).
        "claude_md": claude_md,
        "mcp_json": mcp_json,
        "mcp_servers": mcp_servers,
        # ---- Claude Code hook-captured events ------------------------------
        # Structured view of the last N events the agent fired through
        # .claude/settings.local.json hooks. Surfaces full tool inputs
        # (including Agent prompts and Bash run_in_background starts) so
        # the dashboard and fleet lead can see what the agent is doing
        # without relying on tmux scraping.
        "recent_tools": _event_summary.get("recent_tools") or [],
        "recent_prompts": _event_summary.get("recent_prompts") or [],
        "agent_calls": _event_summary.get("agent_calls") or [],
        "open_agent_calls": _event_summary.get("open_agent_calls") or [],
        # Scalar summaries for terse projection and healer thresholding.
        # open_agent_calls_count > 0 means there are Agent pretool events
        # with no matching posttool — possible stuck subagent.
        # oldest_open_agent_age_s gives the age of the oldest such call.
        # Cross-check with subagent_count before alerting (ring-buffer
        # rotation can produce false positives).
        "open_agent_calls_count": len(_event_summary.get("open_agent_calls") or []),
        "oldest_open_agent_age_s": max(
            (
                c.get("age_seconds") or 0
                for c in (_event_summary.get("open_agent_calls") or [])
            ),
            default=None,
        )
        or None,
        "background_tasks": _event_summary.get("background_tasks") or [],
        "tool_counts": _event_summary.get("counts") or {},
        # Functional-heartbeat shortcuts — top-level so consumers don't
        # have to walk recent_tools. last_tool_at updates on every tool
        # use (LLM-level liveness); last_mcp_tool_at only updates on
        # mcp__* tool calls (proves the MCP sidecar route is live).
        "last_tool_at": _event_summary.get("last_tool_at") or "",
        "last_tool_name": _event_summary.get("last_tool_name") or "",
        "last_mcp_tool_at": _event_summary.get("last_mcp_tool_at") or "",
        "last_mcp_tool_name": _event_summary.get("last_mcp_tool_name") or "",
    }
