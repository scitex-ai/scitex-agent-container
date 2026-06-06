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

Wake-on-push diagnostics (bug #41 hardening, 2026-06-07):
  ``_wire_telegrammer_wake`` (concern (c)) used to silently no-op on every
  misconfig path. Operator complaint: agents look dead on inbound Telegram
  for any of N reasons (missing channel name, missing a2a port, missing
  MCP entry, etc.), with no signal pointing at the root cause. Every skip
  path now emits a WARN-or-ERROR-level log so the operator (or anyone
  reading the runner stderr) can see exactly which gate failed. The
  matching host-side preflight ``validate_telegrammer_wake_wiring`` runs
  in ``_lifecycle/_start.py`` and HARD-ERRORS the start if the channel is
  requested but the wake URL provably won't wire — that catches the
  misconfig BEFORE the agent boots, instead of after the operator's third
  un-replied Telegram message.
"""

from __future__ import annotations

import json as _json
import logging as _logging
import os as _os
from pathlib import Path as _Path

_log = _logging.getLogger(__name__)


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
    has an ``a2a_port`` (so a ``/v1/turn`` endpoint exists). Each skip path
    LOGS the reason at WARN/ERROR (bug #41 hardening, 2026-06-07): a silent
    no-op looks indistinguishable from a bug in the standalone telegrammer JS
    poller, which trapped the operator in a multi-day "why doesn't my agent
    wake?" guessing game. Loud diagnostics make the root cause obvious to
    whoever is reading the runner stderr.
    Never overrides an explicit operator-set TURN_URL in the spec.
    """
    if not any(c.strip() == _TELEGRAMMER_CHANNEL for c in channels):
        # Channel not requested — nothing to wire and nothing to warn about.
        return
    # From here down the operator requested the telegrammer channel, so any
    # gate that drops the wake wiring IS a misconfig worth surfacing.
    if a2a_port is None:
        _log.warning(
            "telegrammer wake NOT wired for %r: spec.a2a.port is unset (None) "
            "but channel %r is requested; without an /v1/turn endpoint the "
            "standalone telegrammer has no URL to POST inbound Telegram "
            "messages to, so an idle agent will not wake on Telegram. Fix: "
            "set spec.a2a.port to 'auto' or an explicit int.",
            _TELEGRAMMER_CHANNEL,
            _TELEGRAMMER_CHANNEL,
        )
        return
    mcps = kwargs.get("mcp_servers")
    if not isinstance(mcps, dict):
        _log.warning(
            "telegrammer wake NOT wired: kwargs['mcp_servers'] is %r, not a "
            "dict; the channel %r is requested but the runner cannot inject "
            "CLAUDE_CODE_TELEGRAMMER_TURN_URL into a malformed mcp_servers "
            "table. Likely caller bug.",
            type(mcps).__name__,
            _TELEGRAMMER_CHANNEL,
        )
        return
    entry = mcps.get(_TELEGRAMMER_MCP_KEY)
    if not isinstance(entry, dict):
        _log.error(
            "telegrammer wake NOT wired: channel %r is requested but no MCP "
            "entry keyed %r found in mcp_servers (current keys: %r). The "
            "agent's to_home/.mcp.json must declare an MCP entry under that "
            "exact key — the standalone bun/ts telegrammer process keyed any "
            "other name will not receive CLAUDE_CODE_TELEGRAMMER_TURN_URL "
            "and an idle agent will not wake on Telegram. Add the entry to "
            "to_home/.mcp.json under the canonical key %r.",
            _TELEGRAMMER_CHANNEL,
            _TELEGRAMMER_MCP_KEY,
            sorted(mcps.keys()),
            _TELEGRAMMER_MCP_KEY,
        )
        return
    env = entry.setdefault("env", {})
    if not isinstance(env, dict):
        _log.warning(
            "telegrammer wake NOT wired: mcp_servers[%r]['env'] is %r, not a "
            "dict; cannot inject CLAUDE_CODE_TELEGRAMMER_TURN_URL. Fix the "
            "to_home/.mcp.json entry's env to be an object.",
            _TELEGRAMMER_MCP_KEY,
            type(env).__name__,
        )
        return
    wired_url = f"http://127.0.0.1:{int(a2a_port)}/v1/turn"
    # Operator-set value wins (explicit > inferred).
    existing = env.get(_TELEGRAMMER_TURN_URL_ENV)
    if existing is not None and existing != wired_url:
        _log.info(
            "telegrammer wake URL pre-set by operator: %r (auto-wired value "
            "would have been %r). Operator override preserved; verify the "
            "pre-set URL actually points at this agent's /v1/turn.",
            existing,
            wired_url,
        )
    env.setdefault(_TELEGRAMMER_TURN_URL_ENV, wired_url)
    _log.info(
        "telegrammer wake wired: %s=%r injected into mcp_servers[%r].env",
        _TELEGRAMMER_TURN_URL_ENV,
        env[_TELEGRAMMER_TURN_URL_ENV],
        _TELEGRAMMER_MCP_KEY,
    )


# ---------------------------------------------------------------------------
# Host-side preflight (called from ``_lifecycle/_start.py``).
#
# The runner-side wake-wiring runs INSIDE the SDK runner after the agent has
# already booted: a loud log there is good but the operator may not be
# tailing the runner stderr. The host-side preflight runs at ``sac agents
# start`` time, BEFORE the runtime is built, and HARD-FAILS the start
# instead of letting the operator discover the misconfig the next time they
# message the bot. Catches the F1 (channel-absent) / F2 (port-absent) /
# F4 (operator override stale) bug shapes loudly at the right place.
#
# This preflight cannot check F3 (MCP entry mis-keyed in to_home/.mcp.json)
# without parsing that file — host-side it would need the agent's
# workspace path; doable as a follow-up but not in this PR's scope. The
# runner-side ``_wire_telegrammer_wake`` ERROR log covers F3 at boot time.
# ---------------------------------------------------------------------------


class TelegrammerWakeWiringError(RuntimeError):
    """The operator requested the telegrammer channel but the wake URL
    provably won't wire; refuse to start so the misconfig is loud.
    """


def validate_telegrammer_wake_wiring(
    channels: list[str] | None,
    a2a_port: int | None,
    *,
    agent_name: str = "",
) -> None:
    """Host-side preflight — raise if the wake wiring is impossible.

    Called from ``_lifecycle/_start.py`` right after ``resolve_a2a_port``
    so the start fails LOUD before any runtime is built, instead of the
    operator discovering the silent no-op via "agent doesn't reply to
    Telegram" hours later.

    Only fires if ``server:claude-code-telegrammer`` is in ``channels``
    AND a downstream gate would have silently no-op'd the wake wiring.
    No-op when the channel is not requested (nothing to validate) or when
    the channel is requested AND the wiring will succeed.

    Catches the host-visible portion of the bug-#41 failure surface:

      F1: channel not requested → silent no-op (we don't fire here at all
          — there's nothing to validate).
      F2: channel requested but ``a2a_port`` is None → would silently
          no-op the runner-side wake wiring; we raise here instead.

    F3 (MCP entry mis-keyed in ``to_home/.mcp.json``) and F4 (operator
    pre-set a stale ``CLAUDE_CODE_TELEGRAMMER_TURN_URL``) are not visible
    here without parsing the to_home tree; the runner-side
    ``_wire_telegrammer_wake`` logs cover those at agent boot.
    """
    if not channels:
        return
    if not any(c.strip() == _TELEGRAMMER_CHANNEL for c in channels):
        return
    if a2a_port is None:
        agent_clause = f" for agent {agent_name!r}" if agent_name else ""
        raise TelegrammerWakeWiringError(
            f"spec.claude.channels{agent_clause} requests "
            f"{_TELEGRAMMER_CHANNEL!r} but spec.a2a.port is unset/null. "
            f"Without an /v1/turn endpoint the standalone telegrammer "
            f"poller has no URL to POST inbound Telegram messages to, so "
            f"an idle agent cannot wake on Telegram. Fix: set spec.a2a.port "
            f"to 'auto' (sac picks a port) or to an explicit free int, then "
            f"retry the start. To run without the wake (legacy "
            f"notifications/claude/channel-only behaviour, only renders for "
            f"already-active turns), remove "
            f"{_TELEGRAMMER_CHANNEL!r} from spec.claude.channels."
        )
