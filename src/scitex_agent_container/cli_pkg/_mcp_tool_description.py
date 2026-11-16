"""Describe one registered tool without invoking its domain operation."""

import json

import click


def show_tool(name: str, *, as_json: bool) -> None:
    """Expose the backend's MCP input schema for an exact tool name."""
    from .mcp_group import _enumerate_tools, _load_get_server

    try:
        tools = _enumerate_tools(_load_get_server()())
    except ImportError as exc:
        raise click.ClickException(
            "MCP dependencies unavailable; install scitex-agent-container[mcp]."
        ) from exc
    tool = next((tool for tool in tools if tool.name == name), None)
    if tool is None:
        raise click.UsageError(f"Unknown MCP tool {name!r}; use `sac mcp list-tools`.")
    convert = getattr(tool, "to_mcp_tool", None)
    if not callable(convert):
        raise click.ClickException(
            "This backend cannot describe the tool's MCP schema."
        )
    record = convert()
    schema = getattr(record, "inputSchema", None)
    if not isinstance(schema, dict):
        raise click.ClickException("The registered tool has no valid MCP input schema.")
    payload = {
        "name": record.name,
        "description": record.description or "",
        "inputSchema": schema,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"{payload['name']}: {payload['description']}")
        click.echo(json.dumps(schema, indent=2))
