"""``--json`` flag resolver."""

from __future__ import annotations

import click


def _json_flag(ctx: click.Context, local: bool) -> bool:
    """Return True if JSON output requested via local flag or top-level --json."""
    return local or bool((ctx.obj or {}).get("json", False))
