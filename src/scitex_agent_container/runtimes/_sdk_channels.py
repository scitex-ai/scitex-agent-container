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
