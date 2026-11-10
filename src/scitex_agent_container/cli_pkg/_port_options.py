"""Shared socket-port admission before CLI previews or lifecycle work."""

import click


def validate_port(port: int, *, option: str = "--port") -> None:
    """Require a socket-port value while retaining zero as an admitted argument."""
    if not 0 <= port <= 65535:
        raise click.UsageError(f"{option} must be between 0 and 65535.")
