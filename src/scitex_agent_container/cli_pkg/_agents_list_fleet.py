"""CLI wiring for the FLEET-WIDE default of ``sac agents list``.

``status_cmds.py`` sits four lines under the per-file cap, so the three new
options and the fleet branch live here and are attached with one decorator.

WHAT CHANGED, AND WHY IT IS A DEFAULT
-------------------------------------
``sac agents list`` used to answer only for the machine it was typed on, so
finding the fleet meant ssh-ing host by host — and every listing quietly
rendered every other host as *nothing there*. Fleet-wide is therefore the
DEFAULT, not a flag: an operator must not have to opt in to being told the
truth about his own fleet, and the exit code never changes because a peer was
unreachable (that would break every caller that parses this command).

``--host`` is the single filter, repeatable, exact-match. There is no
``--here``: ``--host localhost`` covers that case and is RESOLVED at parse
time, with the header echoing the resolution, so no output ever records the
ambiguous claim ``localhost`` — the same ruling that BANNED ``spec.host: local``
("placement must carry the RESOLVED hostname"), applied to a query filter.

``--no-fanout`` is hidden because it is plumbing, not a user-facing filter: the
peer leg runs the peer's OWN ``sac agents list``, which would fan out again,
and again. It is the recursion guard. It doubles as the escape hatch for a
caller that genuinely wants a local-only listing, and the header always says
when it is in force.
"""

from __future__ import annotations

import json as json_mod

import click

__all__ = ["fleet_list_options", "run_fleet_list"]


def fleet_list_options(func):
    """Attach ``--host`` / ``--no-fanout`` / ``--host-timeout`` to a command."""
    from ._helpers._agent_list_fleet_model import DEFAULT_HOST_TIMEOUT_S

    func = click.option(
        "--host-timeout",
        "host_timeout",
        type=float,
        default=DEFAULT_HOST_TIMEOUT_S,
        show_default=True,
        help=(
            "Fleet view: seconds to wait for EACH host. A host that exceeds it "
            "is reported as timed-out in the header and never blocks the rest "
            "of the listing."
        ),
    )(func)
    func = click.option(
        "--no-fanout",
        "no_fanout",
        is_flag=True,
        default=False,
        hidden=True,
        help=(
            "Fleet view: do not query peers; list only this host. Primarily the "
            "recursion guard the fan-out passes to each peer (the peer runs its "
            "own sac, which would fan out again). The header always states when "
            "it is in force, so a local-only listing is never silent."
        ),
    )(func)
    func = click.option(
        "--host",
        "hosts",
        multiple=True,
        metavar="HOSTNAME",
        help=(
            "Fleet view: only this host. Repeatable; exact match on the "
            "resolved hostname. 'localhost' / 'local' are accepted and RESOLVED "
            "at parse time, with the header echoing the resolution. An unknown "
            "name fails loudly, naming every host this machine can reach."
        ),
    )(func)
    return func


def run_fleet_list(
    registry,
    *,
    use_json: bool,
    hosts: tuple[str, ...] = (),
    no_fanout: bool = False,
    host_timeout: float | None = None,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    verbose: bool = False,
    show_all: bool = False,
) -> None:
    """Collect the fleet, print the header, then the rows.

    The header comes FIRST and unconditionally, in both surfaces. It is what
    makes ``agents == []`` legible: with every host answered it means the fleet
    is empty; with a host missing it means the fleet is UNOBSERVED, and those
    two must never render the same way.
    """
    from ._helpers._agent_list import print_agent_list
    from ._helpers._agent_list_fleet import DEFAULT_HOST_TIMEOUT_S, collect_fleet
    from ._helpers._agent_list_fleet_model import UnknownHostFilter
    from ._helpers._agent_list_fleet_render import hosts_payload, print_fleet_header
    from ._helpers._console import console

    show_full = verbose or show_all
    try:
        listing = collect_fleet(
            registry,
            capability=capability,
            machine=machine,
            group=group,
            # The human default hides non-live rows, so the LOCAL leg may skip
            # their (expensive) account + movement enrichment. --json shows
            # every row, so it must stay fully enriched.
            running_only=(not use_json) and not show_full,
            hosts=hosts,
            no_fanout=no_fanout,
            host_timeout_s=(
                DEFAULT_HOST_TIMEOUT_S if host_timeout is None else host_timeout
            ),
        )
    except UnknownHostFilter as exc:
        # Loud, and it names every host that WOULD have worked. A silent empty
        # listing here would render "no such host" exactly like "that host has
        # no agents" — the collapse this whole feature exists to prevent.
        raise click.UsageError(str(exc)) from exc

    if use_json:
        click.echo(
            json_mod.dumps(
                {"agents": listing.agents, "hosts": hosts_payload(listing)},
                indent=2,
            )
        )
        return

    print_fleet_header(console, listing)
    print_agent_list(
        None,
        verbose=verbose,
        show_all=show_all,
        rows=listing.agents,
    )
