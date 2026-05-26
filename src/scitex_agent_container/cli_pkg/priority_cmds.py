"""priority-check and singleton-reconcile commands.

Building blocks for the healer-driven singleton reconciler (scitex-orochi#250).

When an agent declares ``spec.host: [spartan, nas, mba]``, it should run on the
*highest-priority reachable* host, not just wherever ``sac agent start`` was called.
``priority-check`` probes each higher-priority host (via a brief SSH connectivity
check) and returns a JSON report so healer agents can decide whether to initiate
handover. ``singleton-reconcile`` sweeps all locally registered agents and
initiates handover for any that should yield.

Usage:
    sac agent check-priority <config-or-agent-name>
    sac agent check-priority proj-neurovista --current-host nas --json
    sac registry reconcile
    sac registry reconcile --execute  # actually trigger SSH start + local stop
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import click

from .._lifecycle.lifecycle import agent_stop
from .._state.registry import Registry
from ..config import load_config
from ..config._host import resolve_hostname
from ..config._resolve import resolve_with_prefix
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
    # ControlMaster multiplexing (see scitex_agent_container._ssh): reuse
    # one TCP connection per host so parallel probes don't exceed the
    # remote MaxSessions limit or fail due to a read-only control-socket
    # dir inside the SIF.
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=60s",
    "-o",
    "ControlPath="
    + os.path.join(os.environ.get("TMPDIR", "/tmp"), ".sac-ssh-cm", "%C"),
]


def _probe_ssh(host: str) -> bool:
    """Return True if ``host`` is reachable via SSH (``hostname`` exits 0)."""
    _ensure_ssh_control_dir()
    try:
        result = subprocess.run(
            ["ssh"] + _SSH_PROBE_OPTS + [host, "hostname"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _ensure_ssh_control_dir() -> None:
    """Ensure the SSH ControlMaster socket dir exists, importing _ssh lazily."""
    from .._ssh import ensure_control_path_dir

    ensure_control_path_dir()


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


@click.command("check-priority")
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

    \b
    Example:
      $ sac agent check-priority proj-neurovista
      $ sac agent check-priority proj-neurovista --json
    """
    # stx-allow: fallback (reason: config path may not exist or resolve to a valid YAML; CLI exits with code 2 to signal a usage/config error to healer callers)
    try:
        resolved = resolve_with_prefix(config_path)
    except Exception as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}, indent=2))
        else:
            console.print(f"[red]Config not found: {exc}[/red]")
        sys.exit(2)

    if not current_host:
        # stx-allow: fallback (reason: resolve_hostname may fail in non-standard network environments; socket.gethostname() provides a safe alternative that is always available)
        try:
            current_host = resolve_hostname()
        except Exception:
            import socket

            current_host = socket.gethostname().split(".")[0]

    # stx-allow: fallback (reason: priority report computation involves SSH probes that can raise; CLI exits with code 2 so healer scripts can distinguish errors from yield/stay decisions)
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


# ---------------------------------------------------------------------------
# singleton-reconcile: sweep all locally registered agents and yield any
# that have a higher-priority reachable host.  (scitex-orochi#250)
# ---------------------------------------------------------------------------

_SSH_START_TIMEOUT = 30  # seconds to wait for remote sac agent start


def _ssh_start_agent(host: str, agent_name: str) -> bool:
    """SSH to *host* and run ``sac agent start <agent_name>`` in the background.

    Returns True if the remote command exited 0.
    """
    from .._ssh import ensure_control_path_dir, ssh_control_opts

    ensure_control_path_dir()
    cmd = (
        [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "LogLevel=ERROR",
        ]
        + ssh_control_opts()
        + [
            host,
            f"sac agent start {agent_name}",
        ]
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SSH_START_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


@click.command("reconcile-singletons")
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help=(
        "Actually trigger handover: SSH-start the agent on its preferred host, "
        "then stop the local instance. Without this flag, runs dry-run only."
    ),
)
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
    help="Output machine-readable JSON.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Force dry-run (already the default unless --execute is passed).",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompts (currently a no-op; reserved).",
)
def singleton_reconcile(
    execute: bool, current_host: str, as_json: bool, dry_run: bool, yes: bool
) -> None:
    """Reconcile singleton agent placement across the fleet.

    \b
    Note:
      --dry-run is implied by default; --execute is the only flag that opts
      into mutating behaviour. --yes is accepted for API consistency.

    \b
    Example:
      $ sac registry reconcile
      $ sac registry reconcile --execute
      $ sac registry reconcile --json
    """
    _ = dry_run
    _ = yes
    return _singleton_reconcile_body(execute, current_host, as_json)


def _singleton_reconcile_body(execute: bool, current_host: str, as_json: bool) -> None:
    """Reconcile singleton agent placement across the fleet.

    Sweeps all locally registered agents. For each singleton whose YAML
    declares a ``spec.host:`` priority list, runs a priority check. If a
    higher-priority host is reachable (via SSH probe), reports a YIELD
    recommendation. With ``--execute``, initiates handover automatically:
    starts the agent on the preferred host via SSH, then stops it locally.

    Designed to run from a healer agent's periodic loop (every 2–5 min).

    Exit codes:
      0 — all singletons on correct hosts (or --execute succeeded)
      1 — at least one YIELD found (dry-run) or handover failed
      2 — config/runtime error

    \b
    Example:
      $ sac registry reconcile
      $ sac registry reconcile --execute
      $ sac registry reconcile --json
    """
    if not current_host:
        # stx-allow: fallback (reason: resolve_hostname may fail in non-standard environments; gethostname is always available)
        try:
            current_host = resolve_hostname()
        except Exception:
            import socket

            current_host = socket.gethostname().split(".")[0]

    registry = Registry()
    entries = registry.list_all()

    results = []
    any_yield = False
    any_error = False

    for entry in entries:
        name = entry.get("name", "")
        config_path = entry.get("config", "")
        if not config_path:
            continue

        # stx-allow: fallback (reason: malformed YAML or missing config — skip agent and continue sweeping)
        try:
            report = _priority_report(config_path, current_host)
        except Exception as exc:
            results.append(
                {
                    "agent": name,
                    "error": str(exc),
                    "action": "skip",
                }
            )
            any_error = True
            continue

        should = report.get("should_yield", False)
        preferred = report.get("reachable_higher_hosts", [])
        preferred_host = preferred[0] if preferred else report.get("preferred_host", "")
        action = "none"

        if should and preferred_host:
            any_yield = True
            if execute:
                started = _ssh_start_agent(preferred_host, name)
                if started:
                    # stx-allow: fallback (reason: stop failure is non-fatal; agent on preferred host is now running so the priority goal is met even if local stop fails)
                    try:
                        agent_stop(name)
                        action = "yielded"
                    except Exception:
                        action = "remote-started-local-stop-failed"
                        any_error = True
                else:
                    action = "remote-start-failed"
                    any_error = True
            else:
                action = "yield-recommended"
        else:
            action = "stay"

        results.append(
            {
                "agent": name,
                "should_yield": should,
                "preferred_host": preferred_host,
                "current_host": current_host,
                "reason": report.get("reason", ""),
                "action": action,
            }
        )

    if as_json:
        click.echo(json.dumps(results, indent=2))
    else:
        for r in results:
            if "error" in r:
                console.print(f"[yellow]{r['agent']}[/yellow]: error — {r['error']}")
                continue
            agent = r["agent"]
            action = r["action"]
            if action in ("yielded",):
                console.print(
                    f"[green]{agent}[/green]: yielded → {r['preferred_host']} (local stopped)"
                )
            elif action == "yield-recommended":
                console.print(
                    f"[red]{agent}[/red]: YIELD recommended → {r['preferred_host']} "
                    f"(run with --execute to trigger)"
                )
            elif action in ("remote-start-failed", "remote-started-local-stop-failed"):
                console.print(f"[red]{agent}[/red]: handover failed ({action})")
            elif action == "stay":
                console.print(f"[dim]{agent}: stay ({r.get('reason', '')})[/dim]")

    if any_error and not any_yield:
        sys.exit(2)
    if any_yield:
        sys.exit(1 if not execute else (2 if any_error else 0))
    sys.exit(0)
