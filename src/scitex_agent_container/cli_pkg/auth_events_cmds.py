"""``sac auth-events`` — read the fleet auth timeline. READ-ONLY.

The log is JSONL precisely so it can be answered with ``jq``; this command
exists for the two questions nobody should have to write a query for at 3am:

* ``--since`` … what happened around the time everything died?
* ``--unresolved`` … which restarts were attempted and never shown to work?

The second is the one the old rail could not answer at all. A restart log that
only records successes has no reading under which the restarter looks wrong, so
169 ineffective restarts read exactly like 169 recoveries.

This command NEVER writes, restarts, or remediates. It prints.
"""

from __future__ import annotations

import click

__all__ = ["auth_events"]

#: Verdict shown for an attempt with no successful outcome. Deliberately not
#: "FAILED": we know we cannot SHOW it worked, which is a weaker and more
#: honest claim than knowing it did not.
_UNRESOLVED = "UNPROVEN"


def _fmt(event) -> str:
    account = event.account if event.account is not None else "account:unknown"
    agent = event.agent or "-"
    status = f" http={event.http_status}" if event.http_status is not None else ""
    return (
        f"{event.timestamp_utc}  {event.event:<24} {agent:<28} "
        f"{account:<34}{status}  {event.detail or ''}"
    )


@click.command(name="auth-events")
@click.option(
    "--since",
    default=None,
    help="Only events at/after this ISO-8601 UTC prefix (e.g. 2026-07-18T10).",
)
@click.option(
    "--agent", "agent_filter", default=None, help="Only events for this agent."
)
@click.option(
    "--unresolved",
    is_flag=True,
    help="Only restarts ATTEMPTED with no successful outcome recorded.",
)
@click.option(
    "--no-rotations",
    is_flag=True,
    help="Omit credential rotations (they are read from the accounts audit).",
)
@click.option("--limit", default=200, show_default=True, help="Max rows to print.")
def auth_events(
    since: str | None,
    agent_filter: str | None,
    unresolved: bool,
    no_rotations: bool,
    limit: int,
) -> None:
    """Show the collected fleet auth-event timeline.

    Joins this host's auth-event log with the credential rotations already
    recorded by the accounts store, so a rotation and the agents it knocked
    over appear in one ordered reading.
    """
    from .._authevents import unified_timeline, unresolved_attempts

    events = unified_timeline(include_rotations=not no_rotations)
    if unresolved:
        events = unresolved_attempts(events)
    if since:
        events = [e for e in events if (e.timestamp_utc or "") >= since]
    if agent_filter:
        events = [e for e in events if e.agent == agent_filter]

    if not events:
        # Say which of the two empties this is. "Nothing matched" and "nothing
        # was ever recorded" look identical on a silent terminal, and only the
        # first one is evidence about the fleet.
        click.echo(
            "no matching auth events. NOTE: an empty reading is not evidence "
            "that nothing happened — it is evidence that nothing was recorded "
            "here (the rail may not have been running for that window)."
        )
        return

    shown = events[-limit:]
    for event in shown:
        click.echo(_fmt(event))
    if unresolved:
        click.echo(
            f"\n{len(events)} restart attempt(s) {_UNRESOLVED}: attempted with no "
            f"successful outcome recorded. That is 'we cannot show it worked', "
            f"not 'it definitely failed' — re-observation is what settles it."
        )
    if len(events) > len(shown):
        click.echo(f"\n({len(events) - len(shown)} older event(s) not shown)")
