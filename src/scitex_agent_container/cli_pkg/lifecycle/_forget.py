"""Forget inactive registry state without signalling processes or bypassing live rows."""

from __future__ import annotations

import json as _json
from typing import Any

import click

from ..._state.state_store_instances import (
    list_active_instances,
    record_instance_stop,
)
from ..._state.state_store_nodes import unregister_comms_node

__all__ = ["forget"]


_FORGET_EXIT_REASON = "operator-forget"


def _refusal_message(name: str, active_rows: list[dict]) -> str:
    """Name live rows and the normal stop operation required first."""
    hosts = sorted({r.get("host", "?") for r in active_rows})
    return (
        f"refusing to forget {name!r}: the shared store shows {len(active_rows)} "
        f"live instance row(s) on host(s) {hosts!r}. "
        f"Stop the agent with `sac agents stop {name}` before forgetting it."
    )


def _forget_one(name: str, *, force: bool, dry_run: bool) -> dict[str, Any]:
    """Forget a single agent's registry state. Pure-local mutations.

    Returns the per-target envelope dict (the JSON shape ``--json``
    emits). Raises :class:`click.ClickException` on the
    live-row refusal path; idempotent + no-op when nothing to drop.
    """
    if force:
        raise click.ClickException(
            "force is unsupported; stop the agent before forgetting it"
        )
    active = [r for r in list_active_instances() if r.get("name") == name]
    if active:
        raise click.ClickException(_refusal_message(name, active))

    forgotten_rows: list[str] = []
    if not dry_run:
        for row in active:
            instance_id = row.get("id")
            if instance_id and record_instance_stop(
                instance_id, exit_reason=_FORGET_EXIT_REASON
            ):
                forgotten_rows.append(instance_id)
        # comms_nodes is independent — drop the pin regardless of
        # whether an instance row was active (a stale routing tuple
        # without an instance row is exactly the federated-only-stale
        # case ``forget`` exists to clean up).
        try:
            unregister_comms_node(name=name)
        except Exception:  # stx-allow: fallback (reason: a missing comms_nodes row is a legitimate state — name may never have been pinned via the federated graph; never block forget on it)
            pass

    return {
        "name": name,
        "forgotten": True,
        "forgotten_instance_ids": forgotten_rows,
        "exit_reason": _FORGET_EXIT_REASON,
        "dry_run": dry_run,
        "had_live_rows": bool(active),
    }


@click.command(name="forget")
@click.argument("names", type=str, nargs=-1, required=True)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be forgotten without mutating the shared store.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON envelope per agent on stdout.",
)
def forget(
    names: tuple[str, ...],
    dry_run: bool,
    as_json: bool,
) -> None:
    """Drop an agent's registry state locally — no ssh, no signal.

    Recovery verb for the "agent is gone, only stale rows persist"
    case (SLURM-reclaimed node, crashed peer that came back fresh,
    etc.). Unlike ``stop`` / ``delete``, this verb does NOT try to
    reach the agent — it just tombstones the local ``state.db``
    rows and unregisters the federated ``comms_nodes`` pin so
    future routing does not silently fan out to a dead host.

    Refuses to act on a live agent.
    Use ``sac agents stop <name>`` when you want the
    normal remote stop path; use this verb when you
    KNOW there is nothing live to reach.

    \b
    Example:
      $ sac agents forget ghost-agent
      $ sac agents forget ghost-agent --dry-run
      $ sac agents forget ghost-1 ghost-2 --json
    """
    force = False
    any_err = False
    for name in names:
        try:
            envelope = _forget_one(name, force=force, dry_run=dry_run)
        except click.ClickException as exc:
            any_err = True
            if as_json:
                click.echo(
                    _json.dumps(
                        {
                            "name": name,
                            "forgotten": False,
                            "error": exc.format_message(),
                        }
                    )
                )
            else:
                click.echo(f"Error: {exc.format_message()}", err=True)
            continue
        if as_json:
            click.echo(_json.dumps(envelope, ensure_ascii=False))
        else:
            tail = " (dry-run)" if dry_run else ""
            if envelope["had_live_rows"]:
                click.echo(
                    f"forgot {name!r}: tombstoned "
                    f"{len(envelope['forgotten_instance_ids'])} instance "
                    f"row(s){tail}"
                )
            else:
                click.echo(f"forgot {name!r}: nothing to do{tail}")
    if any_err:
        raise SystemExit(1)
