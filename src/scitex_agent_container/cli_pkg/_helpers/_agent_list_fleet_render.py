"""The MANDATORY header above a fleet listing — who answered, who did not, why.

This is the hard requirement of the whole fleet-list change, not decoration.
Without it a table of agents is an unattributed pile: a host that could not be
reached contributes no rows, and *no rows* is indistinguishable from *no agents
there*. The header is what makes an EMPTY fleet and an UNREACHABLE fleet
distinguishable at a glance — and it renders in BOTH surfaces, table and
``--json``, because a machine consumer needs the same distinction a human does.

Three lines, in this order, and the first two only when they have something to
say:

1. ``--host localhost → scitex-compute-04`` — the resolution echo. ``localhost``
   names a different machine depending on where it is typed, so the OUTPUT must
   record the resolved name rather than the ambiguous one.
2. ``5/6 hosts responded — spartan: ssh timed out after 8s`` — the count AND
   every host that did not answer, each with its reason. Green only when the
   whole fleet answered.
3. ``instruments: scitex-compute-04=local_registry, mba=ssh, …`` — WHICH sensor
   produced each host's row set, mirroring the ``evidence[].instrument``
   vocabulary the per-agent liveness verdict publishes. "Unobserved" has been
   reported as "dead" here before; naming the instrument is how a reader tells
   a reading from an assumption.
"""

from __future__ import annotations

from typing import Any

from ._agent_list_fleet_model import NOT_QUERIED, FleetListing

__all__ = [
    "fleet_header_lines",
    "hosts_payload",
    "print_fleet_header",
    "resolution_echo",
    "summary_line",
]


def resolution_echo(listing: FleetListing) -> list[str]:
    """``--host localhost → scitex-compute-04`` for every rewrite that happened."""
    return [
        f"--host {requested} → {resolved}"
        for requested, resolved in listing.resolutions
    ]


def _suppression_note(listing: FleetListing) -> str:
    """Say that peers were NOT ASKED — never let silence imply they were empty.

    Counts only the peers that got NO row of their own: a host the caller named
    with ``--host`` already carries a ``not_queried`` report, and saying it
    twice would make the header noisier without making it truer.
    """
    if not listing.suppressed_reason:
        return ""
    named = sum(1 for r in listing.reports if r.status == NOT_QUERIED)
    n = listing.peers_known - named
    if n <= 0:
        return ""
    noun = "peer" if n == 1 else "peers"
    return f"{n} {noun} NOT queried ({listing.suppressed_reason})"


def summary_line(listing: FleetListing) -> str:
    """``5/6 hosts responded — spartan: ssh timed out after 8s`` (no markup)."""
    total = listing.total
    noun = "host" if total == 1 else "hosts"
    head = f"{listing.responded}/{total} {noun} responded"
    parts = [f"{r.host}: {r.detail}" for r in listing.unanswered]
    note = _suppression_note(listing)
    if note:
        parts.append(note)
    return head + (" — " + "; ".join(parts) if parts else "")


def instrument_line(listing: FleetListing) -> str:
    """``instruments: <host>=<sensor>`` for every host, answered or not."""
    if not listing.reports:
        return ""
    cells = [
        f"{r.host}={r.instrument}" + ("" if r.responded else " (no answer)")
        for r in listing.reports
    ]
    return "instruments: " + ", ".join(cells)


def fleet_header_lines(listing: FleetListing) -> list[str]:
    """Every header line as PLAIN text, in order — the shape ``--json`` echoes.

    Kept separate from :func:`print_fleet_header` so the JSON payload carries
    the exact strings a human is shown. A machine consumer that must decide
    "empty or unobserved?" should read ``hosts.responded`` vs ``hosts.total``;
    these lines are so that a human reading a log of the JSON sees what the
    terminal would have said.
    """
    lines = resolution_echo(listing)
    lines.append(summary_line(listing))
    instruments = instrument_line(listing)
    if instruments:
        lines.append(instruments)
    return lines


def print_fleet_header(console: Any, listing: FleetListing) -> None:
    """Print the header ABOVE the table. Always — there is no quiet mode.

    Colour carries the same three-valued discipline as the text: green only
    when EVERY intended host answered, yellow the moment one did not. It is not
    red — an unreachable peer is not an error, it is an unknown, and dressing it
    as a failure would train the operator to ignore it.
    """
    for line in resolution_echo(listing):
        console.print(f"[cyan]{line}[/cyan]")
    complete = listing.responded == listing.total and not listing.suppressed_reason
    colour = "green" if complete else "yellow"
    console.print(f"[{colour}]{summary_line(listing)}[/{colour}]")
    instruments = instrument_line(listing)
    if instruments:
        console.print(f"[dim]{instruments}[/dim]")


def hosts_payload(listing: FleetListing) -> dict:
    """The ``hosts`` block of ``--json``.

    ``responded`` vs ``total`` is the machine-readable form of the whole point:
    ``agents == []`` with ``responded == total`` is an EMPTY fleet, while
    ``agents == []`` with ``responded < total`` is an UNOBSERVED one. Each
    report additionally carries ``agents: null`` when that host never answered —
    never ``0``, which would be the same lie as omitting it.
    """
    return {
        "responded": listing.responded,
        "total": listing.total,
        "summary": summary_line(listing),
        "header": fleet_header_lines(listing),
        "fanout_suppressed_by": listing.suppressed_reason or None,
        "peers_known": listing.peers_known,
        "filter": {
            "resolutions": [
                {"requested": requested, "resolved": resolved}
                for requested, resolved in listing.resolutions
            ],
            "echo": resolution_echo(listing),
        },
        "reports": [r.to_dict() for r in listing.reports],
    }
