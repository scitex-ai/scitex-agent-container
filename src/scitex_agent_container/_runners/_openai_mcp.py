"""Build ``agents.mcp`` server objects from ``.mcp.json``-shaped config.

Companion to :mod:`_runners.openai_session` (which re-exports
:func:`build_mcp_server`, so callers have one import site). Split out
because transport selection changes when MCP transports change, while the
session changes when the turn lifecycle does — and because inlining it put
``openai_session.py`` one line over the module limit, which was the file
pointing out it had taken on a second job.

WHY THIS EXISTS AT ALL
----------------------
An OpenAI-family sac agent had no tool rail. ``OpenAIAgentsSession`` accepted
in-process ``ToolSpec`` items, but nothing wired the agent's MCP servers, and
the a2a handler passed neither — so a locally-served model could reason about
a task and not touch a file. Measured 2026-08-12: qwen and gpt-oss both emit
correct ``tool_calls`` at the API level, so the missing piece was never the
model.

The config shape is deliberately the SAME ``mcpServers`` object Claude Code
reads from ``.mcp.json``. An agent's servers are declared once and work under
either harness; nobody maintains a second dialect.

``agents`` is imported lazily (the ``[openai]`` extra is optional) — importing
this module must stay side-effect-free on Claude-only deployments.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["McpConfigError", "build_mcp_server"]


class McpConfigError(ValueError):
    """Raised when an MCP entry names no usable transport."""


def default_transports() -> dict[str, Any]:
    """Return the ``agents.mcp`` server class per transport name.

    Imported lazily and only here, so the rest of this module — and its
    tests — need neither the optional ``[openai]`` extra nor any patching
    of import machinery to exercise the interesting logic.
    """
    try:
        from agents.mcp import (
            MCPServerSse,
            MCPServerStdio,
            MCPServerStreamableHttp,
        )
    except Exception as exc:  # stx-allow: fallback (reason: optional dep; broaden beyond ImportError so a misbuilt transitive dep surfaces as an actionable error rather than a bare traceback)
        raise McpConfigError(
            "MCP servers require `openai-agents` "
            "(`pip install scitex-agent-container[openai]`)."
        ) from exc
    return {
        "stdio": MCPServerStdio,
        "sse": MCPServerSse,
        "http": MCPServerStreamableHttp,
    }


def resolve_transport(config: Mapping[str, Any]) -> str:
    """Return ``stdio`` / ``sse`` / ``http`` for one config entry, or ``""``.

    An explicit ``type`` wins. Otherwise the shape decides, the same way
    ``.mcp.json`` distinguishes them in practice: ``command`` means stdio,
    ``url`` means streamable HTTP. Returns ``""`` when neither applies, so
    the caller can raise with the entry's actual keys in hand.

    Pure and dependency-free on purpose — transport selection is the part
    worth testing, and it must be testable without the SDK installed.
    """
    declared = str(config.get("type") or "").strip().lower()
    if declared in ("stdio", "sse", "http"):
        return declared
    if declared in ("streamable-http", "streamable_http"):
        return "http"
    if declared:
        return ""
    if config.get("command"):
        return "stdio"
    if config.get("url"):
        return "http"
    return ""


def build_mcp_server(
    name: str,
    config: Mapping[str, Any],
    *,
    transports: Mapping[str, Any] | None = None,
) -> Any:
    """Build one ``agents.mcp`` server object from a ``.mcp.json`` entry.

    Args:
        name: The server's key in ``mcpServers`` — carried onto the server
            object so SDK errors and tool listings name something the
            operator can find in the config.
        config: The entry. ``{command, args, env}`` for stdio;
            ``{url, headers}`` for sse / streamable HTTP; optional
            ``type`` to force one.
        transports: The server class per transport name. Defaults to
            :func:`default_transports` (the real SDK classes). Injected
            rather than imported at the call site so callers — tests
            included — can supply their own without reaching into import
            machinery: a collaborator swapped by patching is a collaborator
            whose real signature nobody is checking.

    Raises:
        McpConfigError: when the entry names no usable transport, or a
            stdio entry has no ``command``. Fail loud and NAME the server:
            one dropped silently here resurfaces three layers away as "the
            agent ignored its tools", with nothing pointing back at config.

    ``cache_tools_list=True``: the SDK re-fetches the tool list on every
    run otherwise, which for a stdio server is a subprocess round trip per
    turn.
    """
    transport = resolve_transport(config)
    if transport:
        classes = default_transports() if transports is None else transports

    if transport == "stdio":
        command = config.get("command")
        if not command:
            raise McpConfigError(
                f"MCP server {name!r}: transport 'stdio' needs a 'command'."
            )
        params: dict[str, Any] = {"command": command}
        if config.get("args"):
            params["args"] = list(config["args"])
        if config.get("env"):
            params["env"] = dict(config["env"])
        return classes["stdio"](params=params, name=name, cache_tools_list=True)

    if transport in ("sse", "http"):
        url = config.get("url")
        if not url:
            raise McpConfigError(
                f"MCP server {name!r}: transport {transport!r} needs a 'url'."
            )
        params = {"url": url}
        if config.get("headers"):
            params["headers"] = dict(config["headers"])
        return classes[transport](params=params, name=name, cache_tools_list=True)

    raise McpConfigError(
        f"MCP server {name!r}: cannot tell which transport to use from keys "
        f"{sorted(config)!r}. Give it a 'command' (stdio) or a 'url' "
        "(http/sse), or set 'type' explicitly."
    )
