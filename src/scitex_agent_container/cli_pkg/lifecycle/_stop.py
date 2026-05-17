#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents stop`` — stop one or more running agents.

Cross-host dispatch: when an agent's active ``state.db.instances`` row
records ``host != current_host``, ``stop`` ssh's into the peer and runs
``sac agents stop <name> --json`` there, then updates the lead-side row
via :func:`record_instance_stop`. See ``_dispatch.try_dispatch_remote``.
"""

from __future__ import annotations

import json as _json
import shlex
import subprocess
import sys
from pathlib import Path

import click

from ..._lifecycle.lifecycle import agent_stop
from ..._state.host_config import build_ssh_argv
from ..._state.host_config import load as _load_host_config
from ..._state.state_db import now_iso, record_instance_stop
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._common import _iter_agent_yamls
from ._dispatch import try_dispatch_remote


def _dispatch_remote_stop(peer: str, row: dict, peers: dict, name: str) -> dict:
    """SSH into ``peer`` and run ``sac agents stop <name> --json``.

    Updates the lead-side ``instances`` row via :func:`record_instance_stop`
    on success.  Raises ``RuntimeError`` with the full ssh argv + stderr
    on failure (per the project's no-silent-fallback rule).

    Returns the parsed JSON envelope from the peer's stdout.
    """
    ssh_argv = build_ssh_argv(
        peer,
        ["sac", "agents", "stop", name, "--json"],
        peers,
    )
    result = subprocess.run(
        ssh_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Remote `sac agents stop {name}` failed on {peer!r} "
            f"(rc={result.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        envelope = _json.loads(result.stdout)
    except _json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Remote `sac agents stop {name}` on {peer!r} returned "
            f"non-JSON stdout (peer sac may be too old to support "
            f"--json; pull latest on the peer):\n"
            f"stdout (first 500 chars):\n{result.stdout[:500]}\n"
            f"json error: {exc}"
        ) from exc
    # Update lead-side instances row.
    instance_id = row.get("id")
    if instance_id:
        exit_reason = (
            envelope.get("exit_reason") if isinstance(envelope, dict) else None
        ) or "stopped"
        record_instance_stop(instance_id, exit_reason=str(exit_reason))
    return envelope if isinstance(envelope, dict) else {}


@click.command()
@click.argument(
    "targets",
    type=str,
    nargs=-1,
    required=True,
    shell_complete=agent_name_complete,
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Tolerate stale registry, missing configs, and hook failures.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print which agent(s) would be stopped without sending the kill.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt for bulk stop.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit a structured JSON envelope per stopped target on stdout. "
        "Required for cross-host dispatch — the lead parses peer stdout."
    ),
)
def stop(
    targets: tuple[str, ...],
    force: bool,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Stop one or more running agents.

    Each TARGET is an agent name, a YAML path, or a directory containing
    ``<name>/<name>.yaml`` agent layouts. Multiple targets may be given.

    \b
    Example:
      $ sac agent stop foo
      $ sac agent stop foo bar baz
      $ sac agent stop ~/.scitex/agent-container/agents/   # whole dir = bulk
      $ sac agent stop foo --dry-run
      $ sac agent stop foo --json
    """
    # Classify targets: directory targets expand to all <name>/<name>.yaml
    # under them; non-directory targets are agent names or YAML paths.
    single_targets: list[str] = []
    bulk_yamls_from_dirs: list[str] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            for _name, yp in _iter_agent_yamls(p):
                bulk_yamls_from_dirs.append(yp)
        else:
            single_targets.append(t)

    if dry_run:
        for t in single_targets:
            click.echo(f"[dry-run] would stop agent '{t}'")
        for yp in bulk_yamls_from_dirs:
            click.echo(f"[dry-run] would stop agent at '{yp}'")
        return

    # Refuse bulk stop without --yes/-y when directory targets resolved to ≥2 yamls.
    if len(bulk_yamls_from_dirs) > 1 and not yes:
        click.echo(
            f"Refusing to stop {len(bulk_yamls_from_dirs)} agents without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)

    # Resolve all targets to (name, raw) pairs for a unified loop.
    pairs: list[tuple[str, str]] = []
    any_error = False
    for yaml_path in bulk_yamls_from_dirs:
        try:
            config = load_config(yaml_path)
            pairs.append((config.name, yaml_path))
        except Exception as exc:  # stx-allow: fallback (reason: bulk path must continue past per-file load failures)
            any_error = True
            if as_json:
                click.echo(_json.dumps({"target": yaml_path, "error": str(exc)}))
            else:
                console.print(f"[red]Error ({yaml_path}): {exc}[/red]")
    for raw_target in single_targets:
        try:
            name: str = raw_target
            if "/" in name or name.endswith((".yaml", ".yml")):
                config_path = resolve_with_prefix(name)
                config = load_config(config_path)
                name = config.name
            pairs.append((name, raw_target))
        except Exception as exc:  # stx-allow: fallback (reason: name resolution failure must not abort the remaining bulk; surface and continue)
            any_error = True
            if as_json:
                click.echo(_json.dumps({"target": raw_target, "error": str(exc)}))
            else:
                console.print(f"[red]Error ({raw_target}): {exc}[/red]")

    # Dispatch loop — try remote first, fall back to local agent_stop.
    peers = _load_host_config().peers
    for name, raw_target in pairs:
        try:
            envelope_holder: dict = {}

            def _handler(peer, row, ps, _name=name, _holder=envelope_holder):
                _holder.update(_dispatch_remote_stop(peer, row, ps, _name))
                _holder["_peer"] = peer

            dispatched = try_dispatch_remote(name, "stop", peers, handler=_handler)
            if dispatched:
                if as_json:
                    click.echo(
                        _json.dumps(
                            {
                                "name": name,
                                "stopped": True,
                                "host": envelope_holder.get("_peer"),
                                "exit_reason": envelope_holder.get(
                                    "exit_reason", "stopped"
                                ),
                                "ended_at": envelope_holder.get("ended_at"),
                                "dispatched": True,
                            }
                        )
                    )
                else:
                    console.print(
                        f"[green]Agent '{name}' stopped on "
                        f"'{envelope_holder.get('_peer')}'[/green]"
                    )
                continue
            agent_stop(name, force=force)
            if as_json:
                click.echo(
                    _json.dumps(
                        {
                            "name": name,
                            "stopped": True,
                            "exit_reason": "stopped",
                            "ended_at": now_iso(),
                            "dispatched": False,
                        }
                    )
                )
            else:
                console.print(f"[green]Agent '{name}' stopped[/green]")
        except Exception as exc:  # stx-allow: fallback (reason: one stop failure must not abort the remaining targets; surfaces via the per-target JSON envelope or red console line)
            any_error = True
            if as_json:
                click.echo(_json.dumps({"name": name, "error": str(exc)}))
            else:
                console.print(f"[red]Error ({raw_target}): {exc}[/red]")

    if any_error:
        sys.exit(1)


__all__ = ["stop"]
