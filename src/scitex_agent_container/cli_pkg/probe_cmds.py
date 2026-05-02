"""Probe commands: probe-network.

Filed under ``todo#457`` — fleet-side cannot tell "WSL host sleeping"
from "WSL lost internet", so we capture that from inside WSL.
"""

from __future__ import annotations

import json as json_mod
import os
import sys

import click

from ..network_probe import (
    DEFAULT_HUB_HOST,
    DEFAULT_HUB_PORT,
    DEFAULT_HUB_URL,
    run_and_log,
)


@click.command("probe-network")
@click.option(
    "--agent",
    "-a",
    default=None,
    help="Agent name for the JSONL log filename. "
    "Defaults to $SCITEX_OROCHI_AGENT or 'anonymous-agent'.",
)
@click.option(
    "--hub-host",
    default=DEFAULT_HUB_HOST,
    help=f"Hub hostname to probe. Default: {DEFAULT_HUB_HOST}",
)
@click.option(
    "--hub-port",
    default=DEFAULT_HUB_PORT,
    type=int,
    help=f"Hub TCP port. Default: {DEFAULT_HUB_PORT}",
)
@click.option(
    "--hub-url",
    default=DEFAULT_HUB_URL,
    help=f"Hub URL for the HTTPS probe. Default: {DEFAULT_HUB_URL}",
)
@click.option(
    "--timeout",
    default=3.0,
    type=float,
    help="Per-probe timeout in seconds. Default: 3.0",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress stdout — log to JSONL only. Useful from cron.",
)
@click.option(
    "--exit-nonzero-on-fail",
    is_flag=True,
    default=False,
    help="Exit with status 1 if any probe fails. Lets cron/systemd "
    "alert on sustained failure.",
)
def probe_network(
    agent: str | None,
    hub_host: str,
    hub_port: int,
    hub_url: str,
    timeout: float,
    quiet: bool,
    exit_nonzero_on_fail: bool,
) -> None:
    """Probe WSL → fleet-hub connectivity (todo#457).

    Runs four probes (DNS → default gateway → TCP → HTTPS) and writes
    the result as one JSONL line under
    ``~/.scitex/agent-container/logs/network/<agent>.jsonl``.

    The output is designed to be correlated with fleet-side SSH-dead
    logs: when the fleet's SSH probe to this host fails, we have a
    timestamp-aligned ``probes`` record from inside WSL proving which
    layer actually broke (DNS vs LAN vs hub-reach vs TLS).

    \b
    Example:
      $ sac probe-network --agent head-ywata-note-win
      $ sac probe-network --quiet --exit-nonzero-on-fail
    """
    effective_agent = (
        agent
        or os.environ.get("SCITEX_OROCHI_AGENT")
        or os.environ.get("CLAUDE_AGENT_ID")
        or "anonymous-agent"
    )
    summary = run_and_log(
        effective_agent,
        hub_host=hub_host,
        hub_port=hub_port,
        hub_url=hub_url,
        timeout=timeout,
    )
    if not quiet:
        click.echo(json_mod.dumps(summary, indent=2))
    if exit_nonzero_on_fail and not summary["ok"]:
        sys.exit(1)
