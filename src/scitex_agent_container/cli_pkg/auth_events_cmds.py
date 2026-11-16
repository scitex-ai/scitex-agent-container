"""``sac list-auth-events`` — read the fleet auth timeline. READ-ONLY.

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

import json
from copy import copy

import click

__all__ = ["auth_events"]

#: Verdict shown for an attempt with no successful outcome. Deliberately not
#: "FAILED": we know we cannot SHOW it worked, which is a weaker and more
#: honest claim than knowing it did not.
_UNRESOLVED = "UNPROVEN"
_EMPTY_NOTE = (
    "no matching auth events. NOTE: an empty reading is not evidence "
    "that nothing happened — it is evidence that nothing was recorded "
    "here (the rail may not have been running for that window)."
)


def _fmt(event) -> str:
    account = event.account if event.account is not None else "account:unknown"
    agent = event.agent or "-"
    status = f" http={event.http_status}" if event.http_status is not None else ""
    return (
        f"{event.timestamp_utc}  {event.event:<24} {agent:<28} "
        f"{account:<34}{status}  {event.detail or ''}"
    )


@click.command(name="list-auth-events")
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
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=200,
    show_default=True,
    help="Max rows to print.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a machine-readable JSON envelope."
)
@click.pass_context
def auth_events(
    ctx: click.Context,
    since: str | None,
    agent_filter: str | None,
    unresolved: bool,
    no_rotations: bool,
    limit: int,
    as_json: bool,
) -> None:
    """Show the collected fleet auth-event timeline.

    Joins this host's auth-event log with the credential rotations already
    recorded by the accounts store, so a rotation and the agents it knocked
    over appear in one ordered reading.

    \b
    Examples:
        sac list-auth-events --since 2026-07-18T10
        sac list-auth-events --unresolved --json
        sac list-auth-events --no-rotations --limit 20
    """
    from .._authevents import unified_timeline, unresolved_attempts
    from ._helpers._json_flag import _json_flag

    events = unified_timeline(include_rotations=not no_rotations)
    if unresolved:
        events = unresolved_attempts(events)
    if since:
        events = [e for e in events if (e.timestamp_utc or "") >= since]
    if agent_filter:
        events = [e for e in events if e.agent == agent_filter]

    shown = events[-limit:]
    if _json_flag(ctx, as_json):
        # Expose only the observation fields, never arbitrary raw payloads.
        rows = [
            {
                key: getattr(event, key)
                for key in (
                    "timestamp_utc",
                    "event",
                    "agent",
                    "account",
                    "http_status",
                    "detail",
                    "attempt_id",
                    "succeeded",
                )
            }
            for event in shown
        ]
        click.echo(
            json.dumps(
                {
                    "events": rows,
                    "total": len(events),
                    "shown": len(rows),
                    "unresolved": unresolved,
                    "observation_note": _EMPTY_NOTE
                    if not events
                    else (
                        "UNPROVEN: attempted with no successful outcome recorded; "
                        "this is not proof that the restart failed."
                        if unresolved
                        else "Recorded observations only; not a live health check."
                    ),
                },
                indent=2,
            )
        )
        return

    if not events:
        # Say which of the two empties this is. "Nothing matched" and "nothing
        # was ever recorded" look identical on a silent terminal, and only the
        # first one is evidence about the fleet.
        click.echo(_EMPTY_NOTE)
        return

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


_legacy_auth_events = copy(auth_events)
_legacy_auth_events.name = "auth-events"
_legacy_auth_events.hidden = True
_legacy_auth_events.deprecated = "Use list-auth-events instead."
