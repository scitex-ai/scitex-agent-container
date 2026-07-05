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
      per-agent channel work, e.g. an agent running its OWN external channel
      MCP (whose backing stdio MCP the spec author supplies through
      ``to_home/.mcp.json``). The gate was previously hard-coded to
      ``server:sac`` only, so any foreign channel survived all the way down
      the runner argv but was DROPPED here — claude never turned on rendering
      and the notifications were silently ignored (the "store fills, no turn
      appears" silent-failure class).

  (b) ``sac mcp channel`` MCP auto-registration — ``server:sac`` ONLY. That
      sidecar is sac's own bus adapter; it must never be auto-wired for a
      foreign channel. Backing MCPs for non-sac channels come from the
      spec's ``to_home/.mcp.json`` (already merged into ``mcp_servers``).

Wake-on-push (concern (c)) — GENERIC, package-agnostic:
  sac knows the agent's own loopback ``/v1/turn`` endpoint once an a2a port
  is resolved. It EXPOSES that endpoint under the generic, sac-owned env var
  ``SAC_AGENT_TURN_URL`` (see :data:`SAC_AGENT_TURN_URL_ENV`). sac names no
  channel and no downstream package: ANY MCP server entry in the agent's
  ``to_home/.mcp.json`` may OPT IN to wake-on-push by referencing
  ``${SAC_AGENT_TURN_URL}`` in its own ``env`` — the same deploy-time
  ``${VAR}`` substitution the rest of the runtime already uses resolves it
  to ``http://127.0.0.1:<a2a_port>/v1/turn``. sac provides the value; the
  external spec chooses to use it. sac has zero knowledge of what the
  channel is or which env var name the downstream MCP maps it onto.
"""

from __future__ import annotations

import json as _json
import os as _os
import shutil as _shutil
from dataclasses import dataclass
from pathlib import Path as _Path


class SacBinaryNotFoundError(RuntimeError):
    """The ``sac`` console script could not be resolved for the SDK to spawn.

    Raised at MCP-config build time (inside ``apply_channels``) when the
    operator requests ``server:sac`` but no executable ``sac`` exists on
    PATH and no override is set. Letting the MCP config carry an
    unresolvable bare ``sac`` command makes the SDK fail to spawn the
    channel adapter SILENTLY — the agent then has no a2a inbox consumer
    and lead/peer messages accumulate undelivered (smoke-tested
    2026-06-15 on proj-scitex-agent-container: ``sac`` lived at
    ``/opt/venv-agent/bin/sac`` while the SDK's PATH was
    ``/opt/venv-sac/bin:...``, so the MCP child exec'd ``sac`` not found
    and the inbox queued silently for hours). Failing loudly here makes
    the misconfig visible at agent start, not three lead messages later.
    """


# Operator escape hatch: explicit absolute path overrides PATH lookup.
# Set in env when ``sac`` is installed somewhere PATH does not see.
SAC_BIN_ENV = "SAC_BIN"


def _resolve_sac_binary() -> str:
    """Return an absolute, executable path to the ``sac`` console script.

    Resolution order (first hit wins):

      1. ``$SAC_BIN`` env override — must point at an existing executable
         file (raises ``SacBinaryNotFoundError`` when set but unusable, so
         a typo'd override is loud rather than silently falling back to
         the PATH search).
      2. ``shutil.which("sac")`` — standard PATH lookup. Works on any host
         that ships ``sac`` in its PATH (developer workstations, CI).
      3. Known container candidate ``/opt/venv-agent/bin/sac`` — the SAC
         agent-image install location. The SDK runner's PATH does not
         include this directory (it inherits the SAC runtime's PATH which
         is ``/opt/venv-sac/bin:...``), so the PATH search above misses
         it; explicit candidate fixes the spawn for the in-container case.

    Raises ``SacBinaryNotFoundError`` when no candidate resolves.
    """
    override = _os.environ.get(SAC_BIN_ENV)
    if override:
        if _Path(override).is_file() and _os.access(override, _os.X_OK):
            return override
        raise SacBinaryNotFoundError(
            f"{SAC_BIN_ENV}={override!r} is set but the path is not an "
            f"executable file. Fix the env or unset it to fall back to "
            f"PATH lookup."
        )

    found = _shutil.which("sac")
    if found:
        return found

    for candidate in ("/opt/venv-agent/bin/sac",):
        if _Path(candidate).is_file() and _os.access(candidate, _os.X_OK):
            return candidate

    raise SacBinaryNotFoundError(
        "Cannot resolve the `sac` console script: not on PATH and not at "
        "any known candidate location (/opt/venv-agent/bin/sac). The MCP "
        "sidecar config would carry a bare `sac` command that fails exec "
        "silently in the SDK subprocess — the agent's a2a inbox would "
        "have no consumer and lead messages would queue undelivered. "
        f"Fix: install `sac` on PATH, or set ${SAC_BIN_ENV} to its "
        "absolute path."
    )


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
    a per-agent MCP (e.g. an agent's own external channel bot) reaches the
    SDK is via ``ClaudeAgentOptions.mcp_servers`` — which this helper
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


@dataclass(frozen=True)
class ChannelPlan:
    """Runtime-agnostic channel-wiring decisions for one agent.

    Computed once by :func:`compute_channel_plan`, then applied by each
    runtime through its own thin adapter — the SDK to ``ClaudeAgentOptions``
    kwargs (``mcp_servers`` / ``extra_args``), the TUI to the ``apptainer
    exec`` argv (``--mcp-config`` / ``--dangerously-load-development-channels``
    / ``--env``). Centralising the DECISIONS here keeps the two channel-wiring
    paths from drifting; the apply mechanism is the only per-runtime diff.
    """

    channels: tuple[str, ...]
    sac_sidecar_args: tuple[str, ...] | None
    agent_turn_url: str | None


def compute_channel_plan(
    channels: list[str] | None,
    a2a_port: int | None,
    agent_name: str,
) -> ChannelPlan:
    """Resolve ``spec.claude.channels`` into the shared channel-wiring plan.

    Pure — no I/O, no kwargs/argv mutation. Both runtimes wire the SAME
    ``channels`` set (the SDK comma-joins them into the single
    ``--dangerously-load-development-channels`` ``extra_args`` value; the TUI
    emits one flag per entry), so they never disagree on WHICH channels load.

      * ``sac_sidecar_args`` — the ``sac mcp channel`` argv (+ ``--turn-url``
        once an a2a port is resolved), present only when ``server:sac`` is
        requested. The SDK wraps it with sac's resolved abs path; the TUI with
        an in-SIF ``/bin/sh`` resolver — that wrapping is the only diff.
      * ``agent_turn_url`` — the agent's own loopback ``/v1/turn`` endpoint,
        present whenever an a2a port is resolved. sac EXPOSES this generic
        value (under :data:`SAC_AGENT_TURN_URL_ENV`); any external channel MCP
        may opt into wake-on-push by referencing ``${SAC_AGENT_TURN_URL}`` in
        its spec ``env``. Not gated on any specific channel — sac names none.
    """
    chset = _dedupe_channels(channels or [])
    sac_sidecar_args: tuple[str, ...] | None = None
    if any(c.strip() == "server:sac" for c in (channels or [])):
        args = ["mcp", "channel", "--name", agent_name]
        if a2a_port is not None:
            args += ["--turn-url", f"http://127.0.0.1:{int(a2a_port)}/v1/turn"]
        sac_sidecar_args = tuple(args)
    return ChannelPlan(
        channels=tuple(chset),
        sac_sidecar_args=sac_sidecar_args,
        agent_turn_url=agent_turn_url(a2a_port),
    )


def apply_channels(
    kwargs: dict,
    channels: list[str] | None,
    a2a_port: int | None,
    agent_name: str,
) -> None:
    """Wire ``spec.claude.channels`` into the ``ClaudeAgentOptions`` kwargs.

    Mutates ``kwargs`` in place:

      * publishes the agent's own ``/v1/turn`` under ``SAC_AGENT_TURN_URL``
        and resolves any ``${SAC_AGENT_TURN_URL}`` refs left in the merged
        ``mcp_servers`` env (concern (c) — generic wake-on-push, gated only
        on an a2a port being present, NOT on any specific channel);
      * sets ``extra_args["dangerously-load-development-channels"]`` to the
        comma-joined channel set when ANY channel is requested (concern (a));
      * registers the ``sac mcp channel`` stdio MCP under ``mcp_servers["sac"]``
        when ``server:sac`` is among the channels (concern (b)).
    """
    # Concern (c) — GENERIC wake-on-push. The ``$HOME/.mcp.json`` merge
    # (``merge_home_mcp_servers`` in ``_sdk_common``) ran BEFORE the a2a
    # port was known, so any ``${SAC_AGENT_TURN_URL}`` ref in a server
    # entry's env survived unresolved. Publish the value into os.environ
    # (so a spawned MCP that reads it directly sees it) and re-resolve the
    # merged table's ``${VAR}`` refs now that the value is available. sac
    # names no channel/package — an MCP opts in purely by referencing the
    # placeholder. No-op without an a2a port.
    if a2a_port is not None:
        export_agent_turn_url(a2a_port)
        mcps = kwargs.get("mcp_servers")
        if isinstance(mcps, dict):
            kwargs["mcp_servers"] = _resolve_env_refs_local(mcps)

    if not channels:
        return

    plan = compute_channel_plan(channels, a2a_port, agent_name)
    if plan.channels:
        extra_args = kwargs.setdefault("extra_args", {})
        if isinstance(extra_args, dict):
            extra_args.setdefault(
                "dangerously-load-development-channels",
                ",".join(plan.channels),
            )

    if plan.sac_sidecar_args is not None:
        mcps = kwargs.setdefault("mcp_servers", {})
        if isinstance(mcps, dict) and "sac" not in mcps:
            # Resolve `sac` to an absolute path so the SDK subprocess can
            # spawn the channel adapter regardless of its PATH. A bare
            # ``"sac"`` command silently fails exec when the SDK's PATH does
            # not contain the agent venv's bin dir — exactly the bug that
            # left this agent's a2a inbox unsubscribed for hours on
            # 2026-06-15 (see ``_resolve_sac_binary``). The sidecar argv
            # (+ --turn-url) is the shared decision from compute_channel_plan.
            mcps["sac"] = {
                "type": "stdio",
                "command": _resolve_sac_binary(),
                "args": list(plan.sac_sidecar_args),
            }


# ---------------------------------------------------------------------------
# Wake-on-push (concern (c)) — GENERIC, package-agnostic.
#
# sac exposes the agent's own loopback ``/v1/turn`` endpoint under this
# generic, sac-namespaced env var. ANY external channel MCP declared in the
# agent's ``to_home/.mcp.json`` may opt into wake-on-push by referencing
# ``${SAC_AGENT_TURN_URL}`` in its own ``env`` — the deploy-time ``${VAR}``
# substitution (``merge_home_mcp_servers`` for the SDK; the ``--env`` forward
# + claude-CLI expansion for the TUI) then resolves it to the agent's turn
# URL. sac provides the VALUE; the spec chooses to use it. sac has zero
# knowledge of which channel/package references it or what env var name that
# package maps the value onto downstream.
# ---------------------------------------------------------------------------
SAC_AGENT_TURN_URL_ENV = "SAC_AGENT_TURN_URL"


def agent_turn_url(a2a_port: int | None) -> str | None:
    """The agent's own loopback ``/v1/turn`` URL, or ``None`` w/o a port.

    Deploy-time-resolvable value sac knows once ``spec.a2a.port`` is set.
    A push POSTed here advances an IDLE runner session (wake-on-push).
    """
    if a2a_port is None:
        return None
    return f"http://127.0.0.1:{int(a2a_port)}/v1/turn"


def export_agent_turn_url(a2a_port: int | None) -> str | None:
    """Publish the agent's turn URL into ``os.environ`` for ``${VAR}`` refs.

    Sets ``SAC_AGENT_TURN_URL`` in the process env (``setdefault`` — an
    operator/spec pre-set value wins) so that ``${SAC_AGENT_TURN_URL}`` refs
    in the merged ``$HOME/.mcp.json`` resolve at merge time via
    ``_resolve_env_refs_local``. Returns the resolved URL (or ``None`` when
    no a2a port is available, in which case nothing is exported). sac names
    no channel: any MCP entry that references the placeholder opts in.
    """
    url = agent_turn_url(a2a_port)
    if url is not None:
        _os.environ.setdefault(SAC_AGENT_TURN_URL_ENV, url)
    return url
