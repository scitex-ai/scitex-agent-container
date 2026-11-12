"""Argument-only preview for host configuration pushes; no state providers."""

from __future__ import annotations

import json
from typing import Any

import click

from ._helpers import _json_flag


def emit_preview(ctx: click.Context, *, as_json: bool, **arguments: Any) -> None:
    """Describe selectors and options without resolving peers or reading tokens."""
    payload = {
        "operation": "host.push-config",
        "dry_run": True,
        "scope": "arguments_only",
        "verified": False,
        **arguments,
    }
    click.echo(json.dumps(payload, indent=2))
    if not _json_flag(ctx, as_json):
        click.echo(
            "Preview only: no config/token read, SSH, write or listener restart."
        )
