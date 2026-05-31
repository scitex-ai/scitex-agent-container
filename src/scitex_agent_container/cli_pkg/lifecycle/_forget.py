"""``sac agents forget`` — local-only registry-reset recovery (backlog #3).

Operator backlog #3 (per lead 2026-06-01). Today there is no verb that
drops a specific agent's registry state cleanly when the agent is
*already gone* but ``state.db`` still claims it is running. The
existing ``sac agents stop --force`` handles "agent WAS running, peer
now unreachable" (it shells ssh, catches transport failure, then
force-releases the local binding). But it does NOT handle:

* a SLURM-reclaimed compute node whose agent never got a clean stop
* a peer that came back with a fresh ``state.db`` (the lead's view of
  the agent's binding is stale, but there is nothing to ssh to)
* an operator who knows the agent is dead and just wants the entry
  gone without going through the ssh + stop dance

The dispatch fixes #252/#253 do not close this gap — they fix the
``stop --force`` path's tolerance, not the case where there is
nothing live to stop in the first place.

``forget`` is the registry-reset recovery verb:

* tombstones the ``instances`` row with
  ``exit_reason='operator-forget'`` (distinct from ``stopped`` /
  ``peer-unreachable-force-released`` / ``cleanup`` so post-hoc
  state.db forensics tells these apart)
* unregisters the ``comms_nodes`` row so future a2a routing does
  not silently fan out to the dead host
* NO ssh, NO local process signal — purely local state.db mutations
* refuses to act on an agent that has a live ``instances`` row
  unless ``--force`` is passed (avoids accidental state-clobber on
  a healthy agent the operator forgot was running)

The verb is idempotent: running it on a name with no rows at all
exits 0 with a "nothing-to-do" envelope.
"""

from __future__ import annotations

import json as _json
from typing import Any

import click

from ..._state.state_db_instances import (
    list_active_instances,
    record_instance_stop,
)
from ..._state.state_db_nodes import unregister_comms_node

__all__ = ["forget"]


_FORGET_EXIT_REASON = "operator-forget"


def _refusal_message(name: str, active_rows: list[dict]) -> str:
    """Build the operator-facing refusal when --force is missing.

    Names the live instance(s), the remedy (``--force``), and the
    safer-alternative (``sac agents stop --force``) so the operator
    chooses with full context.
    """
    hosts = sorted({r.get("host", "?") for r in active_rows})
    return (
        f"refusing to forget {name!r}: state.db shows {len(active_rows)} "
        f"live instance row(s) on host(s) {hosts!r}. If you are SURE the "
        f"agent is gone and want the rows dropped anyway, re-run with "
        f"--force. If the agent is reachable, prefer "
        f"`sac agents stop --force {name}` instead — it tries the remote "
        f"stop first and only force-releases on transport failure."
    )


def _forget_one(name: str, *, force: bool, dry_run: bool) -> dict[str, Any]:
    """Forget a single agent's registry state. Pure-local mutations.

    Returns the per-target envelope dict (the JSON shape ``--json``
    emits). Raises :class:`click.ClickException` on the no-force +
    live-row refusal path; idempotent + no-op when nothing to drop.
    """
    active = [r for r in list_active_instances() if r.get("name") == name]
    if active and not force:
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
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Forget even if state.db shows the agent as live. Without "
        "--force, a live instance row aborts (use `sac agents stop "
        "--force <name>` to try the remote stop first)."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be forgotten without mutating state.db.",
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
    force: bool,
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

    Refuses to act on a live agent unless ``--force`` is passed.
    Use ``sac agents stop --force <name>`` when you want the
    remote-stop-then-force-release path; use this verb when you
    KNOW there is nothing live to reach.

    \b
    Example:
      $ sac agents forget ghost-agent --force
      $ sac agents forget ghost-agent --dry-run
      $ sac agents forget ghost-1 ghost-2 --force --json
    """
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
