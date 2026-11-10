"""Argument-only recovery plans; no fleet observation or state recording."""

from __future__ import annotations

import json
from typing import Any

import click


def emit_preview(action: str, **arguments: Any) -> None:
    """Describe requested recovery options without invoking either pass."""
    click.echo(
        json.dumps(
            {
                "action": action,
                "dry_run": True,
                "scope": "arguments_only",
                "verified": False,
                **arguments,
            },
            indent=2,
        )
    )
