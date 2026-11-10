"""Pure option admission before an HTTP MCP preview or backend import."""

import click


def validate_http_port(use_http: bool, port: int) -> None:
    """Validate HTTP bounds while preserving stdio's ignored port option."""
    if use_http and not 0 <= port <= 65535:
        raise click.UsageError("--port must be between 0 and 65535 for HTTP transport.")
