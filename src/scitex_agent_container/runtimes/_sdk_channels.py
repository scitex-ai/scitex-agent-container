"""``spec.claude.channels`` → claude dev-channels flag + sac MCP sidecar.

Extracted from ``_sdk_common.build_sdk_options`` so the channel wiring has
one focused home (and its own test surface).

claude renders ``<channel ...>`` tags — the ONLY way a channel notification
ADVANCES a turn in an SDK session — solely when the bundled ``claude`` binary
is started with ``--dangerously-load-development-channels`` listing the
channel set. ``apply_channels`` sets that flag and, for ``server:sac``,
auto-registers sac's own bus-adapter MCP.

Two separate concerns, gated independently:

  (a) dev-channels flag — fire for ANY ``spec.claude.channels`` entry, value
      = comma-joined set of every requested channel. This is what lets a
      per-agent channel work, e.g. an agent running its OWN telegrammer bot
      via ``server:claude-code-telegrammer`` (whose backing stdio MCP the
      spec author supplies through ``to_home/.mcp.json``). The gate was
      previously hard-coded to ``server:sac`` only, so any foreign channel
      survived all the way down the runner argv but was DROPPED here —
      claude never turned on rendering and the notifications were silently
      ignored (the "store fills, no turn appears" silent-failure class).

  (b) ``sac mcp channel`` MCP auto-registration — ``server:sac`` ONLY. That
      sidecar is sac's own bus adapter; it must never be auto-wired for a
      foreign channel. Backing MCPs for non-sac channels come from the
      spec's ``to_home/.mcp.json`` (already merged into ``mcp_servers``).
"""

from __future__ import annotations

import json as _json
import os as _os
from pathlib import Path as _Path


def merge_home_mcp_servers(mcp_servers: dict) -> dict:
    """Merge ``$HOME/.mcp.json`` MCP servers into ``mcp_servers``.

    ``to_home/.mcp.json`` deploys to the container ``$HOME/.mcp.json``
    (see ``_to_home.py`` / skill 25 — the documented per-agent MCP
    delivery). But the apptainer SDK runner runs INSIDE the container
    where ``resolve_agent_workspace`` cannot find the agent's mcp config:
    the in-container registry lookup fails AND the config's ``workdir``
    is the HOST path (absent in-container), so it returns ``{}``. The
    SDK's own project-scope ``.mcp.json`` discovery is also dead because
    the runner sets ``setting_sources=[]`` (verified: a ``/work/.mcp.json``
    is NOT loaded under empty setting_sources). So the ONLY reliable way
    a per-agent MCP (e.g. an agent's own telegrammer bot) reaches the SDK
    is via ``ClaudeAgentOptions.mcp_servers`` — which this helper
    populates from the to_home-deployed ``$HOME/.mcp.json``.

    Best-effort: a missing/malformed file yields the input unchanged.
    ``resolve_agent_workspace`` entries (passed in as ``mcp_servers``)
    win on key collision — explicit registry config beats the file.
    ``${VAR}`` refs in entry values resolve from ``os.environ``.
    """
    home = _os.environ.get("HOME")
    if not home:
        return mcp_servers
    path = _Path(home) / ".mcp.json"
    if not path.is_file():
        return mcp_servers
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return mcp_servers
    servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
    if not isinstance(servers, dict) or not servers:
        return mcp_servers

    merged = dict(mcp_servers)
    for name, entry in servers.items():
        if name in merged or not isinstance(entry, dict):
            continue  # registry config wins; skip non-dict junk
        e = _resolve_env_refs_local(dict(entry))
        e.setdefault("type", "stdio")
        merged[name] = e
    return merged


def _resolve_env_refs_local(value):
    """Resolve ``${VAR}`` refs from os.environ, recursively (str/list/dict)."""
    import re as _re

    if isinstance(value, str):
        return _re.sub(
            r"\$\{(\w+)\}",
            lambda m: _os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _resolve_env_refs_local(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs_local(v) for v in value]
    return value


def _dedupe_channels(channels: list[str]) -> list[str]:
    """Return the channel names stripped + deduped, preserving spec order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in channels:
        name = raw.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def apply_channels(
    kwargs: dict,
    channels: list[str] | None,
    a2a_port: int | None,
    agent_name: str,
) -> None:
    """Wire ``spec.claude.channels`` into the ``ClaudeAgentOptions`` kwargs.

    Mutates ``kwargs`` in place:

      * sets ``extra_args["dangerously-load-development-channels"]`` to the
        comma-joined channel set when ANY channel is requested (concern (a));
      * registers the ``sac mcp channel`` stdio MCP under ``mcp_servers["sac"]``
        when ``server:sac`` is among the channels (concern (b)).

    No-op when ``channels`` is empty/None.
    """
    if not channels:
        return

    chset = _dedupe_channels(channels)
    if chset:
        extra_args = kwargs.setdefault("extra_args", {})
        if isinstance(extra_args, dict):
            extra_args.setdefault(
                "dangerously-load-development-channels", ",".join(chset)
            )

    if any(c.strip() == "server:sac" for c in channels):
        mcps = kwargs.setdefault("mcp_servers", {})
        if isinstance(mcps, dict) and "sac" not in mcps:
            # Sidecar subscribes to the BUS inbox (`sac listen`), resolved
            # from SAC_LISTEN_BASE_URL — NOT the a2a port. a2a_port is the
            # WAKE path (WI-1): passed as --turn-url so a received bus event
            # POSTs to the agent's own loopback /v1/turn to wake an idle
            # session (push ≡ Telegram).
            sidecar_args = ["mcp", "channel", "--name", agent_name]
            if a2a_port is not None:
                sidecar_args += [
                    "--turn-url",
                    f"http://127.0.0.1:{int(a2a_port)}/v1/turn",
                ]
            mcps["sac"] = {
                "type": "stdio",
                "command": "sac",
                "args": sidecar_args,
            }

    _wire_telegrammer_wake(kwargs, channels, a2a_port)


# Channel name for the standalone claude-code-telegrammer MCP a per-agent
# bot rides on (its backing MCP comes from the agent's to_home/.mcp.json,
# keyed ``claude-code-telegrammer``).
_TELEGRAMMER_CHANNEL = "server:claude-code-telegrammer"
_TELEGRAMMER_MCP_KEY = "claude-code-telegrammer"
# Env var the telegrammer poller reads to enable wake-on-push (POST inbound
# to the agent's own /v1/turn). See claude-code-telegrammer ts/lib/wake.ts.
_TELEGRAMMER_TURN_URL_ENV = "CLAUDE_CODE_TELEGRAMMER_TURN_URL"


def _wire_telegrammer_wake(
    kwargs: dict,
    channels: list[str],
    a2a_port: int | None,
) -> None:
    """Concern (c): wake-on-push for the ``server:claude-code-telegrammer``
    channel — symmetric with the ``server:sac`` ``--turn-url`` above.

    The telegrammer MCP (an agent's OWN Telegram bot, backed by the spec's
    ``to_home/.mcp.json``) only emits ``notifications/claude/channel``. That
    renders ``<channel>`` for an ACTIVE turn but does NOT advance an IDLE
    SDK-runner session — the same limitation the ``sac mcp channel``
    ``--turn-url`` removes for ``server:sac``. Inbound messages then pile up
    unread (the "store fills, no turn appears" silent-failure class).

    Fix: inject ``CLAUDE_CODE_TELEGRAMMER_TURN_URL`` into the telegrammer MCP
    entry's env, pointing at the agent's own loopback ``/v1/turn``. The
    telegrammer poller (ts/lib/wake.ts) then POSTs each inbound message there
    and the runner drives a turn at once (push ≡ the lead's Telegram channel).

    Gated: only when the channel set requests the telegrammer channel, the
    merged ``mcp_servers`` actually carries the backing entry, and the runner
    has an ``a2a_port`` (so a ``/v1/turn`` endpoint exists). No-op otherwise —
    e.g. an interactive CLI with no a2a port keeps the notification-only path.
    Never overrides an explicit operator-set TURN_URL in the spec.
    """
    if not any(c.strip() == _TELEGRAMMER_CHANNEL for c in channels):
        return
    if a2a_port is None:
        return
    mcps = kwargs.get("mcp_servers")
    if not isinstance(mcps, dict):
        return
    entry = mcps.get(_TELEGRAMMER_MCP_KEY)
    if not isinstance(entry, dict):
        return
    env = entry.setdefault("env", {})
    if not isinstance(env, dict):
        return
    # Operator-set value wins (explicit > inferred).
    env.setdefault(
        _TELEGRAMMER_TURN_URL_ENV,
        f"http://127.0.0.1:{int(a2a_port)}/v1/turn",
    )
