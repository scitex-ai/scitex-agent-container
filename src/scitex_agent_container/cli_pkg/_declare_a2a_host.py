"""``sac agents declare-a2a-host`` — make every spec state its own bind address.

Home: the ``agents`` noun-group, Maintenance section, beside ``refresh-acl``.
That verb is the closest existing thing — a fleet-wide sweep that reads every
``~/.scitex/agent-container/agents/*/spec.yaml`` and is safe to re-run — and it
already established both the registry-dir resolution and the ``--dry-run``
surface this reuses. ``sac dev`` was the alternative and is wrong: this is a
fleet operation an operator runs, not a contributor tool. A new top-level noun
for one verb would be worse still.

DRY-RUN IS THE DEFAULT, per this package's standing rule (see
``_maintenance/__init__``: "report by default, mutate only on request").
``--apply`` is the deliberate act.

What it writes, and why that is not a behaviour change
------------------------------------------------------
It writes ``host: 127.0.0.1`` — :data:`..config._a2a_defaults.DEFAULT_A2A_HOST`,
the value every reader of ``spec.a2a.host`` already falls back to when the key
is absent. A spec that gains this line resolves to the same host it resolved to
before, through the same code. There is deliberately NO ``--host`` option: an
operator-supplied value would make the command capable of changing what agents
bind to, and "zero behaviour change" would become a property of how it was
invoked rather than of the command. Rebinding an agent is a different
operation and deserves its own PR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from ._helpers import console
from .refresh_acl import _fleet_registry_dir


def _archive_dir(registry: Path) -> Path:
    """A timestamped archive beside the registry — the rollback path.

    Under ``.old/`` so it never looks like an agent directory to the very glob
    this command uses to enumerate the fleet.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return registry / ".old" / f"declare-a2a-host-{stamp}"


def _headline(plan) -> str:
    """A one-line verdict.

    Deliberately NOT ``plan.summary()``. That renders every refused agent by
    name, which is right for a migration where refusals are rare and each one
    needs a human — and useless here, where 101 of 102 specs already satisfy
    the sweep and the summary becomes a 101-name wall that buries the single
    spec being changed. The names are still printed, grouped by reason, below.
    """
    parts = [f"{len(plan.edits)} spec(s) scanned", f"{len(plan.writable)} to write"]
    if plan.refused:
        parts.append(f"{len(plan.refused)} refused")
    if plan.malformed:
        parts.append(f"{len(plan.malformed)} MALFORMED")
    if plan.unreadable:
        parts.append(f"{len(plan.unreadable)} unreadable")
    return "; ".join(parts)


def _report(plan, host: str) -> None:
    from .._maintenance._spec_sweep_plan import group_refusals

    for reason, agents in group_refusals(plan).items():
        shown = ", ".join(agents[:6]) + (" …" if len(agents) > 6 else "")
        console.print(f"  [dim]{len(agents):>3} {reason}[/dim] ({shown})")
    for edit in plan.writable:
        console.print(f"  [green]+[/green] {edit.agent}: spec.a2a.host: {host}")
    if plan.malformed:
        console.print(
            f"  [red]{len(plan.malformed)} MALFORMED[/red] — the editor would "
            "change more than the one intended line: "
            + ", ".join(e.agent for e in plan.malformed)
        )
    for entry in plan.unreadable:
        console.print(f"  [red]UNREADABLE[/red] {entry}")


@click.command(name="declare-a2a-host")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Actually write. Without it this reports what WOULD change and exits.",
)
def declare_a2a_host(apply_: bool) -> None:
    """Make every fleet spec declare its a2a bind address explicitly.

    Adds ``host: 127.0.0.1`` to the ``spec.a2a`` block of any spec that omits
    it — the same value the code already defaults to, so nothing an agent binds
    changes. Specs that already declare it are left byte-identical, so the
    sweep is safe to re-run.

    Writes are transactional: every target is archived first, and if the
    post-write re-parse finds any spec whose host is wrong or whose document
    drifted, ALL of them are restored.

    \b
    Example:
      $ sac agents declare-a2a-host            # dry-run (default)
      $ sac agents declare-a2a-host --apply
    """
    from .._maintenance._a2a_host_sweep import (
        parse_specs,
        plan_a2a_host_sweep,
        verify_hosts,
    )
    from .._maintenance._layers_migration_apply import apply_migration
    from ..config._a2a_defaults import DEFAULT_A2A_HOST

    registry = _fleet_registry_dir()
    if not registry.is_dir():
        raise click.ClickException(
            f"Fleet registry dir not found: {registry}. Expected the "
            "user-scope agents registry at ~/.scitex/agent-container/agents/."
        )

    plan = plan_a2a_host_sweep(registry, DEFAULT_A2A_HOST)
    console.print(f"[bold]{_headline(plan)}[/bold]")
    _report(plan, DEFAULT_A2A_HOST)

    if not apply_:
        console.print(
            "\n[dim]Dry-run — nothing written. Re-run with --apply to write.[/dim]"
        )
        # A dry-run that FOUND a problem must not exit 0; a caller scripting
        # this needs the plan's own verdict, not just its prose.
        raise SystemExit(0 if plan.safe_to_apply else 1)

    if not plan.writable:
        console.print("\n[green]Every spec already declares it. Nothing to do.[/green]")
        return

    archive = _archive_dir(registry)
    before = parse_specs(plan)
    result = apply_migration(
        plan, archive, lambda: verify_hosts(plan, before, DEFAULT_A2A_HOST)
    )

    if result.refused:
        console.print(f"\n[red]REFUSED[/red] — nothing written: {result.refused}")
        raise SystemExit(1)
    if result.rolled_back:
        console.print(
            f"\n[red]ROLLED BACK[/red] — verification failed after writing, "
            f"originals restored from {archive}: {result.rolled_back}"
        )
        raise SystemExit(1)

    console.print(
        f"\n[green]Wrote {len(result.written)} spec(s)[/green]; "
        f"originals archived at {archive}"
    )


__all__ = ["declare_a2a_host"]
