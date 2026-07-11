#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents stop`` — stop one or more running agents.

Cross-host dispatch: when an agent's active ``state.db.instances`` row
records ``host != current_host``, ``stop`` ssh's into the peer and runs
``sac agents stop <name> --json`` there, then updates the lead-side row
via :func:`record_instance_stop`. See ``_dispatch.try_dispatch_remote``.

When NO active row exists at all (e.g. the agent was started BY the peer
itself so this caller never recorded one), the SPEC's ``host:`` pin routes
the stop instead (``_host_routing.spec_host_fallback_peer``) — transparent
remote routing, operator directive 2026-07-10. A spec pinned to an
UNREGISTERED host fails loud with the peer list rather than erroring
locally with a misleading "not running".
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
from ..._state.state_db_comms_nodes import unregister_comms_node
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._common import _iter_agent_yamls
from ._dispatch import try_dispatch_remote
from ._host_routing import spec_host_fallback_peer

# Stable exit_reason marker for the release-on-unreachable path so a
# follow-up audit can grep state.db for stale-binding releases and
# distinguish them from clean stops.
_FORCE_RELEASED_EXIT_REASON = "peer-unreachable-force-released"


class _PeerUnreachableError(RuntimeError):
    """Raised when ``_dispatch_remote_stop`` cannot reach the bound peer
    via ssh — distinct from generic ``RuntimeError`` so the caller can
    decide to release the stale binding locally (with ``--force``) rather
    than aborting the whole stop loop on a transport failure.

    Carries the full message that would have been raised (so the caller
    can still echo it to the operator when force-release fires), plus a
    flag indicating whether the failure was at the ssh transport layer
    (rc != 0 with no parseable JSON envelope, or no stdout at all) — i.e.
    the peer is genuinely unreachable, not "the remote sac returned an
    error envelope we should respect".
    """

    def __init__(self, message: str, *, peer: str) -> None:
        super().__init__(message)
        self.peer = peer


def _force_release_binding(name: str, row: dict, peer: str) -> dict:
    """Tombstone the lead-side instances row + remove the comms_nodes
    pin so a subsequent start can re-bind the singleton to its current
    spec.host.

    Called from the stop loop when ``_dispatch_remote_stop`` raises
    :class:`_PeerUnreachableError` AND the operator passed ``--force``.
    The release MUST clear BOTH stores — the user's bm025 repro hung on
    the unreachable peer precisely because the singleton's instance row
    AND comms_nodes binding both still pointed there, so any subsequent
    routing (``stop``, ``send``, ``--on``-propagated ``start``) re-tried
    the dead host. Without comms_nodes also being cleared, future a2a
    routing would still try bm025 even after the instance row was
    closed.

    Returns the envelope the caller would otherwise have parsed from the
    peer, so the per-target JSON shape stays stable.
    """
    instance_id = row.get("id")
    if instance_id:
        record_instance_stop(instance_id, exit_reason=_FORCE_RELEASED_EXIT_REASON)
    # stx-allow: fallback (reason: a missing comms_nodes row is a
    # legitimate state — the singleton may never have been pinned via
    # the federated graph — and must not block the release)
    try:
        unregister_comms_node(name=name)
    except Exception:
        pass
    return {
        "name": name,
        "stopped": True,
        "force_released": True,
        "host": peer,
        "exit_reason": _FORCE_RELEASED_EXIT_REASON,
        "ended_at": now_iso(),
        "dispatched": False,
    }


def _dispatch_remote_stop(peer: str, row: dict, peers: dict, name: str) -> dict:
    """SSH into ``peer`` and run ``sac agents stop <name> --json``.

    Updates the lead-side ``instances`` row via :func:`record_instance_stop`
    on success. Raises :class:`_PeerUnreachableError` when the ssh
    transport itself fails (rc != 0; covers
    ``Connection refused`` / ``pam_slurm_adopt denied`` / hostname
    resolution failure / etc.) so the caller can fall through to
    :func:`_force_release_binding` under ``--force``. Raises plain
    ``RuntimeError`` for ``--json`` parse failures (peer sac responded
    but spoke a different shape — operator must reconcile, not
    auto-release).

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
        raise _PeerUnreachableError(
            f"Remote `sac agents stop {name}` failed on {peer!r} "
            f"(rc={result.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
            peer=peer,
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
            release_holder: dict = {}

            def _handler(
                peer,
                row,
                ps,
                _name=name,
                _holder=envelope_holder,
                _release=release_holder,
                _force=force,
            ):
                try:
                    _holder.update(_dispatch_remote_stop(peer, row, ps, _name))
                    _holder["_peer"] = peer
                except _PeerUnreachableError as exc:
                    if not _force:
                        # No force → surface the ssh failure unchanged.
                        # The outer try/except records it as an error.
                        raise
                    # --force on an unreachable peer: release the stale
                    # binding locally so the operator isn't blocked by
                    # an unreachable prior host (the lead's bm025
                    # repro). The release writes BOTH the instances
                    # tombstone AND the comms_nodes pin removal so
                    # subsequent routing re-binds to the current
                    # spec.host.
                    _release.update(_force_release_binding(_name, row, peer))
                    _release["_underlying_error"] = str(exc)

            dispatched = try_dispatch_remote(name, "stop", peers, handler=_handler)
            if not dispatched:
                # No instances row anywhere → fall back to the SPEC's host
                # pin (transparent remote routing). Reuses the exact
                # row-driven handler with an empty row (no lead-side id to
                # tombstone). UnknownSpecHostError propagates to the
                # per-target except below — loud, with the peer list.
                spec_peer = spec_host_fallback_peer(name, peers, verb="stop")
                if spec_peer is not None:
                    _handler(spec_peer, {}, peers)
                    dispatched = True
            if dispatched:
                if release_holder:
                    # Force-released path.
                    if as_json:
                        click.echo(_json.dumps(release_holder))
                    else:
                        console.print(
                            f"[yellow]Agent '{name}' force-released on "
                            f"'{release_holder.get('host')}' "
                            f"(peer unreachable; "
                            f"{_FORCE_RELEASED_EXIT_REASON})[/yellow]"
                        )
                    continue
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
