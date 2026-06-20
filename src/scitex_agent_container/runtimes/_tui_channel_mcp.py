"""Register ``spec.claude.channels`` backing MCP servers into ``$HOME/.claude.json``.

Closes the SDK↔TUI channel-resolution drift (handoff item 3, 2026-06-20):
a spec with ``channels: [server:sac]`` boots into the TUI showing
``server:sac · no MCP server configured with that name`` even though
:func:`_apptainer_inner_argv.tui_channel_config` already passes the sac
subscriber on an inline ``--mcp-config``.

Why the inline ``--mcp-config`` is NOT enough (verified against the
SDK-bundled ``claude`` v2.1.150 binary — the same the SIF ships):

  claude resolves each ``--dangerously-load-development-channels`` entry to a
  registered MCP server BY NAME. The resolver (binary fn ``zo_``) only scans
  MCP servers from the SETTINGS scopes — ``enterprise`` / ``user`` /
  ``project`` / ``local``. Those come from ``t2(scope).servers``:

    * ``project`` — ``.mcp.json`` files walking UP from cwd (``--pwd``);
    * ``user``    — the top-level ``mcpServers`` of ``$HOME/.claude.json``;
    * ``local``   — ``$HOME/.claude.json``'s ``projects[<cwd>].mcpServers``.

  Servers passed via ``--mcp-config`` (the inline sac subscriber AND the
  ``--mcp-config $HOME/.mcp.json`` file the TUI loads) are stored SEPARATELY
  (binary ``K.mcpConfig``) and are NEVER part of the scope set the channel
  resolver checks. So the channel never binds — a2a wake, telegram, and
  scitex-todo notifications are all silently lost (the "store fills, no turn
  appears" class). The ``server:`` prefix on the flag value is CORRECT
  (the binary's parser keys on it: ``pK.startsWith("server:")`` → ``{kind:
  "server", name: pK.slice(7)}``, and an UNtagged entry errors with
  ``entries must be tagged``), so the fix is NOT the flag — it is registering
  the backing MCP in a scope the resolver reads.

This module writes the backing MCP servers into the agent's OWN
``$HOME/.claude.json`` ``mcpServers`` (the ``user`` scope) — never an
operator project dir. For a ``server:<name>`` channel:

  * ``server:sac`` → the ``sac mcp channel`` subscriber, resolved at spawn
    time across the SIF venvs by :func:`_apptainer_inner_argv._sac_channel_mcp_server`
    (the SAME entry the inline ``--mcp-config`` carries; shared decision via
    :func:`_apptainer_inner_argv.tui_channel_plan` so the two never drift).
  * any other ``server:<name>`` → the backing MCP the agent ships in
    ``to_home/.mcp.json`` (deployed to ``$HOME/.mcp.json``), copied across
    under the bare name ``<name>``. FAIL-LOUD when that backing entry is
    absent: a declared channel with no resolvable MCP is a misconfig the
    operator must see, not a silent drop (matches ``_sdk_channels``'
    wake-wiring diagnostics + the fail-loud doctrine).

Idempotent: an existing ``mcpServers[<name>]`` is never clobbered (operator
config wins). The matching SDK path registers the same servers via
``ClaudeAgentOptions.mcp_servers`` (``apply_channels``); this is its TUI
mirror.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ChannelMcpMissingError",
    "build_channel_mcp_servers",
    "ensure_tui_channel_mcp",
    "register_channels_into_claude_json",
]

# Notation prefix a ``spec.claude.channels`` entry carries (``server:sac``).
# claude strips it internally to the bare MCP name it resolves the channel
# against, so we strip it here to key the registered server identically.
_SERVER_PREFIX = "server:"

# The channel name whose backing MCP is sac's own bus subscriber (handled
# specially — its entry is synthesised, not read from to_home/.mcp.json).
_SAC_CHANNEL = "server:sac"


class ChannelMcpMissingError(RuntimeError):
    """A declared ``server:<name>`` channel has no resolvable backing MCP.

    Raised when the spec lists a channel whose MCP server is neither sac's
    own subscriber nor present in the agent's ``$HOME/.mcp.json`` (deployed
    from ``to_home/.mcp.json``). Failing loud here surfaces the misconfig at
    agent start instead of as the silent ``server:<name> · no MCP server
    configured with that name`` warning the operator only notices when the
    agent never replies.
    """


def _channel_mcp_name(channel: str) -> str:
    """Map a ``spec.claude.channels`` entry to its bare MCP server name."""
    name = channel.strip()
    if name.startswith(_SERVER_PREFIX):
        return name[len(_SERVER_PREFIX) :]
    return name


def _read_home_mcp_servers(home_dir: Path) -> dict:
    """Return ``mcpServers`` from ``<home_dir>/.mcp.json`` (or ``{}``).

    The backing MCPs for non-sac channels (e.g. the agent's own telegrammer
    bot) live here, deployed from ``to_home/.mcp.json`` by ``deploy_to_home``.
    A missing file is the legitimate no-backing case (the caller fails loud
    only for a channel that genuinely has no entry); a malformed file is a
    hard error so a typo in the agent's own ``.mcp.json`` is loud.
    """
    path = home_dir / ".mcp.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
    return servers if isinstance(servers, dict) else {}


def build_channel_mcp_servers(config: AgentConfig, home_dir: Path) -> dict[str, dict]:
    """Resolve declared channels → ``{mcp_name: entry}`` for ``.claude.json``.

    One entry per ``spec.claude.channels`` member, keyed by the bare MCP name
    (``server:`` stripped) so it matches what claude resolves the channel
    against. ``server:sac`` synthesises the ``sac mcp channel`` subscriber
    (shared with the inline ``--mcp-config`` via ``tui_channel_plan``); every
    other channel copies its backing entry from the agent's ``$HOME/.mcp.json``
    (deployed from ``to_home/.mcp.json``) or, failing that, from the spec's
    own ``spec.mcp_servers`` block.

    Raises :class:`ChannelMcpMissingError` for a declared channel with no
    resolvable backing MCP (no silent drop). Empty dict when no channels are
    declared.
    """
    # Local import: _apptainer_inner_argv imports _sdk_channels at module
    # scope and this module is imported by tui_session; keep the channel-plan
    # dependency lazy to avoid any import-order coupling.
    from ._apptainer_inner_argv import _sac_channel_mcp_server, tui_channel_plan

    claude_spec = getattr(config, "claude", None)
    channels = [
        str(c).strip()
        for c in (getattr(claude_spec, "channels", []) or [])
        if str(c).strip()
    ]
    if not channels:
        return {}

    plan = tui_channel_plan(config)
    # Backing-MCP sources for non-sac channels, in precedence order: the
    # deployed $HOME/.mcp.json (what the TUI actually loads) wins, then the
    # spec's declarative ``spec.mcp_servers`` block (v2). A channel resolves
    # if EITHER carries the bare-named entry.
    home_servers = _read_home_mcp_servers(home_dir)
    spec_servers = getattr(config, "mcp_servers", None)
    spec_servers = spec_servers if isinstance(spec_servers, dict) else {}

    out: dict[str, dict] = {}
    for channel in channels:
        name = _channel_mcp_name(channel)
        if not name:
            continue
        if channel.strip() == _SAC_CHANNEL:
            # The sac subscriber — synthesised from the SAME shared plan the
            # inline --mcp-config uses (in-SIF resolver + --turn-url). The
            # plan always carries sac_sidecar_args when server:sac is present.
            if plan.sac_sidecar_args is None:  # pragma: no cover — defensive
                continue
            out[name] = _sac_channel_mcp_server(list(plan.sac_sidecar_args))
            continue
        entry = home_servers.get(name)
        if not isinstance(entry, dict):
            entry = spec_servers.get(name)
        if not isinstance(entry, dict):
            raise ChannelMcpMissingError(
                f"agent {getattr(config, 'name', '?')!r} declares channel "
                f"{channel!r} but no MCP server keyed {name!r} is present in "
                f"its $HOME/.mcp.json (deployed from to_home/.mcp.json) or "
                f"spec.mcp_servers (home keys: {sorted(home_servers)}; spec "
                f"keys: {sorted(spec_servers)}). claude resolves a channel to "
                f"a registered MCP server BY NAME, so without that entry the "
                f"TUI shows '{channel} · no MCP server configured with that "
                f"name' and the channel is silently dropped (inbound "
                f"a2a/telegram/todo never arrive). Add the MCP under the exact "
                f"key {name!r} to the agent's to_home/.mcp.json."
            )
        out[name] = dict(entry)
    return out


def register_channels_into_claude_json(config: AgentConfig, home_dir: Path) -> bool:
    """Merge the channels' backing MCP servers into ``<home_dir>/.claude.json``.

    Writes into the top-level ``mcpServers`` (claude's ``user`` scope — the
    one the channel resolver reads) and adds each name to
    ``enabledMcpjsonServers`` so the TUI does not prompt to enable it. An
    existing same-name entry is preserved (operator config wins). Atomic
    write (temp + rename). No-op (returns ``False``) when no channels resolve.

    ``home_dir`` must already exist (the caller materialised it). Returns
    ``True`` iff the file was written.
    """
    servers = build_channel_mcp_servers(config, home_dir)
    if not servers:
        return False

    claude_json = home_dir / ".claude.json"
    data: dict = {}
    if claude_json.is_file():
        # A malformed .claude.json is a hard error (no silent fallback): the
        # onboarding seeder writes it, so an unparseable file is a real bug.
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}

    mcp_servers = data.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        data["mcpServers"] = mcp_servers
    enabled = data.setdefault("enabledMcpjsonServers", [])
    if not isinstance(enabled, list):
        enabled = []
        data["enabledMcpjsonServers"] = enabled

    changed = False
    for name, entry in servers.items():
        if name not in mcp_servers:
            mcp_servers[name] = entry
            changed = True
        if name not in enabled:
            enabled.append(name)
            changed = True

    if not changed:
        return False

    tmp = claude_json.with_suffix(".json.sac-channel.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(claude_json)
    logger.info(
        "tui channels: registered MCP server(s) %s into %s (user scope) so "
        "claude resolves spec.claude.channels",
        ", ".join(sorted(servers)),
        claude_json,
    )
    return True


def ensure_tui_channel_mcp(config: AgentConfig, *home_dirs: Path | None) -> None:
    """Register channel-backing MCPs into every materialised agent home.

    Called from ``TuiSessionRuntime.materialize_workspace`` after the homes
    are populated. Runs over BOTH the workspace-home and (for relaxed
    directory-overlay specs) the overlay upper-home — the same pair
    ``ensure_project_onboarding`` writes — so the registration lands
    regardless of which home-delivery mode the spec uses. ``None`` /
    non-existent dirs are skipped.

    Propagates :class:`ChannelMcpMissingError` (fail loud) — a declared
    channel with no backing MCP must stop the start, not boot an agent whose
    comms silently never wire.
    """
    for home_dir in home_dirs:
        if home_dir is None or not home_dir.is_dir():
            continue
        register_channels_into_claude_json(config, home_dir)
