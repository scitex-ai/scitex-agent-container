"""priority-check command: report whether this host should yield to a higher-priority host.

Building block for the healer-driven singleton reconciler (scitex-orochi#250).

When an agent declares ``spec.host: [spartan, nas, mba]``, it should run on the
*highest-priority reachable* host, not just wherever ``sac start`` was called.
This command probes each higher-priority host (via a brief SSH connectivity check)
and returns a JSON report so healer agents can decide whether to initiate handover.

Usage:
    sac priority-check <config-or-agent-name>
    sac priority-check proj-neurovista --current-host nas --json
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

import click

from ..config import load_config, resolve_config
from ..config._host import resolve_hostname
from ._helpers import console

# Lightweight SSH reachability options — no TTY, short timeout, no host-key prompt.
_SSH_PROBE_OPTS = [
    "-o",
    "ConnectTimeout=3",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "LogLevel=ERROR",
]


def _probe_ssh(host: str) -> bool:
    """Return True if ``host`` is reachable via SSH (``hostname`` exits 0)."""
    try:
        result = subprocess.run(
            ["ssh"] + _SSH_PROBE_OPTS + [host, "hostname"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _priority_report(
    config_path: str,
    current_host: str,
) -> dict:
    """Build the priority report dict for a given agent YAML and host."""
    config = load_config(config_path)
    spec = config.hosts_spec

    # Multi-instance agents don't have a single preferred host.
    if spec.hosts:
        return {
            "agent": config.name,
            "mode": "multi-instance",
            "should_yield": False,
            "reason": "multi-instance agents run everywhere — no priority ordering",
            "current_host": current_host,
        }

    host_val = spec.host
    if not host_val:
        return {
            "agent": config.name,
            "mode": "local-singleton",
            "should_yield": False,
            "reason": "no host preference declared — agent runs anywhere",
            "current_host": current_host,
        }

    chain: list[str] = [host_val] if isinstance(host_val, str) else list(host_val)
    if not chain:
        return {
            "agent": config.name,
            "mode": "local-singleton",
            "should_yield": False,
            "reason": "empty host chain",
            "current_host": current_host,
        }

    preferred_host = chain[0]
    if current_host == preferred_host:
        return {
            "agent": config.name,
            "mode": "singleton",
            "should_yield": False,
            "reason": "already on highest-priority host",
            "current_host": current_host,
            "current_rank": 1,
            "preferred_host": preferred_host,
            "host_chain": chain,
        }

    if current_host not in chain:
        return {
            "agent": config.name,
            "mode": "singleton",
            "should_yield": False,
            "reason": (
                f"current host '{current_host}' is not in the priority chain "
                f"{chain!r} — agent should not be running here at all"
            ),
            "current_host": current_host,
            "host_chain": chain,
        }

    current_rank = chain.index(current_host) + 1  # 1-based
    higher_priority = chain[: current_rank - 1]

    reachable: list[str] = []
    unreachable: list[str] = []
    for h in higher_priority:
        if _probe_ssh(h):
            reachable.append(h)
        else:
            unreachable.append(h)

    should_yield = bool(reachable)
    if should_yield:
        reason = (
            f"higher-priority host(s) are reachable: {reachable!r}; "
            f"current host '{current_host}' (rank {current_rank}) should yield"
        )
    else:
        reason = (
            f"current host '{current_host}' (rank {current_rank}) is the highest "
            f"reachable — higher hosts {unreachable!r} are unreachable"
        )

    return {
        "agent": config.name,
        "mode": "singleton",
        "should_yield": should_yield,
        "reason": reason,
        "current_host": current_host,
        "current_rank": current_rank,
        "preferred_host": preferred_host,
        "host_chain": chain,
        "higher_priority_hosts": higher_priority,
        "reachable_higher_hosts": reachable,
        "unreachable_higher_hosts": unreachable,
    }


@click.command("priority-check")
@click.argument("config_path")
@click.option(
    "--current-host",
    default="",
    help="Override the detected hostname (default: live hostname()).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON (default: human summary).",
)
def priority_check(
    config_path: str,
    current_host: str,
    as_json: bool,
) -> None:
    """Report whether this host should yield a singleton agent to a higher-priority host.

    CONFIG_PATH is a YAML file path or agent name (resolved via sac's config search).

    Exit codes:
      0  — should NOT yield (current host is correct or preferred is unreachable)
      1  — SHOULD yield (a higher-priority host is reachable)
      2  — error (config not found, YAML invalid, etc.)

    Example — run from a healer's periodic tick:
      if sac priority-check proj-neurovista; then
        echo "OK"
      else
        echo "Need to hand off to higher-priority host"
      fi
    """
    try:
        resolved = resolve_config(config_path)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}, indent=2))
        else:
            console.print(f"[red]Config not found: {exc}[/red]")
        sys.exit(2)

    if not current_host:
        try:
            current_host = resolve_hostname()
        except Exception:
            import socket

            current_host = socket.gethostname().split(".")[0]

    try:
        report = _priority_report(resolved, current_host)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}, indent=2))
        else:
            console.print(f"[red]Error building priority report: {exc}[/red]")
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        should = report.get("should_yield", False)
        symbol = "[red]YIELD[/red]" if should else "[green]STAY[/green]"
        console.print(f"[bold]{report['agent']}[/bold]: {symbol}")
        console.print(f"  {report.get('reason', '')}")
        if "reachable_higher_hosts" in report:
            console.print(
                f"  chain: {report.get('host_chain', [])}"
                f"  reachable-higher: {report.get('reachable_higher_hosts', [])}"
            )

    sys.exit(1 if report.get("should_yield") else 0)
