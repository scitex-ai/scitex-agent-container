"""Argument-only listener plans, without token or runtime state lookup."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

import click


def show_listen_preview(
    action: Literal["start", "stop", "restart"],
    *,
    bind: str,
    token_file: Path | None = None,
    allow_non_loopback: bool = False,
    print_token: bool = False,
    grace_secs: float | None = None,
) -> None:
    """Print a parsed argument plan; this establishes no lifecycle readiness."""
    from .listen_cmds import _is_loopback, _split_bind

    try:
        host, port = _split_bind(bind)
    except ValueError as exc:
        raise click.UsageError("--bind must contain a host and integer port.") from exc
    if action == "start" and not _is_loopback(host) and not allow_non_loopback:
        raise click.UsageError(
            f"--bind {host}:{port} is not loopback. Pass --allow-non-loopback "
            "if you have an external tunnel arranged."
        )
    if grace_secs is not None and not math.isfinite(grace_secs):
        raise click.UsageError("--grace-secs must be finite for argument preview.")
    options = (
        {
            "token_file": str(token_file) if token_file is not None else None,
            "allow_non_loopback": allow_non_loopback,
            "print_token": print_token,
        }
        if action == "start"
        else {"grace_secs": grace_secs}
    )
    click.echo(
        json.dumps(
            {
                "action": action,
                "target": "listen",
                "dry_run": True,
                "scope": "arguments_only",
                "bind": bind,
                "host": host,
                "port": port,
                "options": options,
                "unverified": [
                    "token defaults and credential material",
                    "daemon, pidfile and socket state",
                    "supervisor and failover state",
                    "lifecycle outcome",
                ],
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
    )
